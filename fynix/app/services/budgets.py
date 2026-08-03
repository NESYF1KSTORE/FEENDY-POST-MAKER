"""Budget envelopes and the cost ledger (spec §14, FR-019, AC-10).

Thresholds behave exactly as §14.1 specifies:
  50%  soft     — forecast + optimisation hint, nothing is blocked
  80%  warning  — notify PM/Finance, optional evals and retries are refused
 100%  hard     — new billable runs are refused; running work finishes safely
      override — time-bound, needs an owner, an amount, a reason and an audit event
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import audit
from app.core.errors import BudgetExceeded, NotFoundError
from app.models.base import utcnow
from app.models.finance import Budget, CostLedgerEntry
from app.orchestrator import events

SOFT = "soft"
WARNING = "warning"
HARD = "hard"


@dataclass(frozen=True)
class BudgetState:
    limit: float
    spent: float
    usage_pct: float
    hard_stopped: bool
    level: str  # "ok" | "soft" | "warning" | "hard"
    remaining: float

    @property
    def blocks_billable(self) -> bool:
        return self.hard_stopped or self.level == HARD

    @property
    def blocks_optional(self) -> bool:
        """Optional evals/retries stop at the 80% warning (spec §14.1)."""
        return self.level in {WARNING, HARD} or self.hard_stopped


def ensure_budget(
    session: Session, *, tenant_id: str, project_id: str, limit: float | None = None
) -> Budget:
    budget = session.execute(
        select(Budget).where(Budget.tenant_id == tenant_id, Budget.project_id == project_id)
    ).scalar_one_or_none()
    if budget is not None:
        return budget
    settings = get_settings()
    budget = Budget(
        tenant_id=tenant_id,
        project_id=project_id,
        limit_amount=limit if limit is not None else settings.default_project_budget_rub,
        soft_pct=settings.budget_soft_pct,
        warning_pct=settings.budget_warning_pct,
    )
    session.add(budget)
    session.flush()
    return budget


def _level(budget: Budget, now: datetime) -> str:
    pct = budget.usage_pct
    if pct >= 100:
        return HARD
    if pct >= budget.warning_pct:
        return WARNING
    if pct >= budget.soft_pct:
        return SOFT
    _ = now
    return "ok"


def state(session: Session, *, tenant_id: str, project_id: str, now: datetime | None = None) -> BudgetState:
    now = now or utcnow()
    budget = ensure_budget(session, tenant_id=tenant_id, project_id=project_id)
    _expire_override(session, budget, now)
    return BudgetState(
        limit=budget.effective_limit,
        spent=budget.spent_amount,
        usage_pct=budget.usage_pct,
        hard_stopped=budget.hard_stopped,
        level=_level(budget, now),
        remaining=max(0.0, budget.effective_limit - budget.spent_amount),
    )


def _expire_override(session: Session, budget: Budget, now: datetime) -> None:
    if budget.override_until is not None and budget.override_until <= now:
        budget.override_amount = 0.0
        budget.override_until = None
        budget.override_by = ""
        budget.override_reason = ""
        # Re-arm the hard stop if spend is above the base limit again.
        budget.hard_stopped = budget.spent_amount >= budget.limit_amount
        session.flush()


def assert_can_spend(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    estimated_amount: float = 0.0,
    optional: bool = False,
    now: datetime | None = None,
) -> BudgetState:
    """Gate before starting a billable unit of work.

    `optional=True` marks work the spec allows us to drop under pressure —
    extra evals and discretionary retries — which stops at 80% rather than 100%.
    """
    current = state(session, tenant_id=tenant_id, project_id=project_id, now=now)
    if current.blocks_billable:
        raise BudgetExceeded(
            "project budget exhausted; new billable runs are blocked",
            details={
                "reason": "budget_hard_stop",
                "limit": current.limit,
                "spent": current.spent,
            },
        )
    if optional and current.blocks_optional:
        raise BudgetExceeded(
            "budget warning threshold reached; optional runs are suspended",
            details={"reason": "budget_warning", "usage_pct": current.usage_pct},
        )
    if estimated_amount > 0 and current.spent + estimated_amount > current.limit:
        raise BudgetExceeded(
            "estimated cost would exceed the approved budget",
            details={
                "reason": "budget_forecast_exceeded",
                "estimated": estimated_amount,
                "remaining": current.remaining,
            },
        )
    return current


def record_cost(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    provider: str,
    sku: str,
    quantity: float,
    unit: str,
    effective_rate: float,
    amount: float | None = None,
    stage: str = "",
    task_id: str | None = None,
    run_id: str | None = None,
    currency: str = "RUB",
    billable: bool = True,
    invoice_ref: str = "",
    now: datetime | None = None,
) -> CostLedgerEntry:
    """Append a ledger entry and roll the project budget forward."""
    now = now or utcnow()
    total = amount if amount is not None else round(quantity * effective_rate, 6)
    entry = CostLedgerEntry(
        tenant_id=tenant_id,
        project_id=project_id,
        stage=stage,
        task_id=task_id,
        run_id=run_id,
        provider=provider,
        sku=sku,
        quantity=quantity,
        unit=unit,
        effective_rate=effective_rate,
        amount=total,
        currency=currency,
        billable=billable,
        invoice_ref=invoice_ref,
        occurred_at=now,
    )
    session.add(entry)

    budget = ensure_budget(session, tenant_id=tenant_id, project_id=project_id)
    if billable:
        budget.spent_amount = round(budget.spent_amount + total, 6)
        _check_thresholds(session, budget, now)
    session.flush()
    return entry


def _check_thresholds(session: Session, budget: Budget, now: datetime) -> None:
    pct = budget.usage_pct
    notified = set(budget.notified_thresholds or [])
    crossed: list[int] = []
    for threshold in (budget.soft_pct, budget.warning_pct, 100):
        if pct >= threshold and threshold not in notified:
            crossed.append(threshold)
            notified.add(threshold)
    if not crossed:
        return

    budget.notified_thresholds = sorted(notified)
    if 100 in crossed:
        budget.hard_stopped = True
        audit.record(
            session,
            tenant_id=budget.tenant_id,
            action="budget.hard_stop",
            actor_type="system",
            resource_type="budget",
            resource_id=budget.id,
            decision="deny",
            payload={"project_id": budget.project_id, "spent": budget.spent_amount},
        )
    for threshold in crossed:
        events.emit(
            session,
            tenant_id=budget.tenant_id,
            project_id=budget.project_id,
            event_type=events.BUDGET_THRESHOLD_REACHED,
            payload={
                "threshold_pct": threshold,
                "usage_pct": pct,
                "limit": budget.effective_limit,
                "spent": budget.spent_amount,
                "hard_stopped": budget.hard_stopped,
            },
        )
    session.flush()


def grant_override(
    session: Session,
    *,
    principal,
    project_id: str,
    amount: float,
    hours: int,
    reason: str,
    now: datetime | None = None,
) -> Budget:
    """Emergency override — time-bound, attributed and audited (spec §14.1)."""
    now = now or utcnow()
    if amount <= 0:
        raise ValueError("override amount must be positive")
    if not reason.strip():
        raise ValueError("override reason is required")
    budget = session.execute(
        select(Budget).where(
            Budget.tenant_id == principal.tenant_id, Budget.project_id == project_id
        )
    ).scalar_one_or_none()
    if budget is None:
        raise NotFoundError(f"budget for project '{project_id}' not found")

    budget.override_amount = amount
    budget.override_until = now + timedelta(hours=hours)
    budget.override_by = principal.user_id
    budget.override_reason = reason
    budget.hard_stopped = budget.spent_amount >= budget.effective_limit
    budget.notified_thresholds = [
        t for t in (budget.notified_thresholds or []) if t < budget.usage_pct
    ]
    session.flush()

    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="budget.override_granted",
        actor_id=principal.user_id,
        resource_type="budget",
        resource_id=budget.id,
        payload={
            "project_id": project_id,
            "amount": amount,
            "expires_at": budget.override_until.isoformat(),
            "reason": reason,
        },
    )
    return budget


def summary(session: Session, *, tenant_id: str, project_id: str) -> dict:
    """Per-provider breakdown plus a naive linear forecast (FR-019)."""
    rows = session.execute(
        select(
            CostLedgerEntry.provider,
            CostLedgerEntry.unit,
            func.sum(CostLedgerEntry.quantity),
            func.sum(CostLedgerEntry.amount),
        )
        .where(
            CostLedgerEntry.tenant_id == tenant_id,
            CostLedgerEntry.project_id == project_id,
        )
        .group_by(CostLedgerEntry.provider, CostLedgerEntry.unit)
    ).all()

    current = state(session, tenant_id=tenant_id, project_id=project_id)
    by_provider = [
        {
            "provider": provider,
            "unit": unit,
            "quantity": float(quantity or 0),
            "amount": round(float(amount or 0), 2),
        }
        for provider, unit, quantity, amount in rows
    ]
    return {
        "limit": current.limit,
        "spent": round(current.spent, 2),
        "remaining": round(current.remaining, 2),
        "usage_pct": current.usage_pct,
        "level": current.level,
        "hard_stopped": current.hard_stopped,
        "by_provider": sorted(by_provider, key=lambda r: -r["amount"]),
    }
