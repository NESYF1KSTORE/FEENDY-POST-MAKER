"""Approval workflow (FR-005, spec §2.1).

Rules implemented here:
  * requirements come from the approval matrix, never from the caller;
  * approvals are collected in sequence groups — a later group is only offered
    once every earlier one is approved;
  * an approval is bound to the checksum of what it approved, so changing the
    subject marks it stale (spec §3.1);
  * an approved-but-expired signature does not count.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import ConflictError, NotFoundError, PermissionDenied
from app.core.policy import (
    ApprovalRequirement,
    approval_expiry,
    approval_requirement,
    can_decide,
)
from app.models.base import ApprovalStatus, Role, utcnow
from app.models.governance import Approval
from app.orchestrator import events


def request_approvals(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    subject_type: str,
    subject_id: str,
    subject_checksum: str,
    requested_by: str = "",
    now: datetime | None = None,
) -> list[Approval]:
    """Create the pending approvals demanded by the matrix for `subject_type`.

    Re-requesting for the same subject supersedes any outstanding pending rows,
    so a retried workflow step does not accumulate duplicates (spec §3.1).
    """
    now = now or utcnow()
    requirement: ApprovalRequirement = approval_requirement(subject_type)

    existing = (
        session.execute(
            select(Approval).where(
                Approval.tenant_id == tenant_id,
                Approval.subject_type == subject_type,
                Approval.subject_id == subject_id,
                Approval.status == ApprovalStatus.PENDING.value,
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        row.status = ApprovalStatus.STALE.value

    created: list[Approval] = []
    for role, sequence in requirement.as_pairs():
        approval = Approval(
            tenant_id=tenant_id,
            project_id=project_id,
            subject_type=subject_type,
            subject_id=subject_id,
            required_role=role.value,
            sequence=sequence,
            subject_checksum=subject_checksum,
            expires_at=approval_expiry(requirement, now),
        )
        session.add(approval)
        created.append(approval)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="approval.requested",
        actor_id=requested_by,
        resource_type=subject_type,
        resource_id=subject_id,
        payload={
            "rule": requirement.rule,
            "roles": [r.value for r, _ in requirement.as_pairs()],
        },
    )
    events.emit(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        event_type=events.BLUEPRINT_APPROVAL_REQUESTED
        if subject_type == "blueprint"
        else "approval.requested",
        payload={
            "subject_type": subject_type,
            "subject_id": subject_id,
            "approval_ids": [a.id for a in created],
        },
    )
    return created


def list_for_subject(
    session: Session, *, tenant_id: str, subject_type: str, subject_id: str
) -> list[Approval]:
    return list(
        session.execute(
            select(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.subject_type == subject_type,
                Approval.subject_id == subject_id,
            )
            .order_by(Approval.sequence.asc(), Approval.created_at.asc())
        )
        .scalars()
        .all()
    )


def actionable(approvals: list[Approval]) -> list[Approval]:
    """Pending approvals whose earlier sequence groups are already satisfied."""
    by_seq: dict[int, list[Approval]] = {}
    for approval in approvals:
        if approval.status in {ApprovalStatus.STALE.value, ApprovalStatus.EXPIRED.value}:
            continue
        by_seq.setdefault(approval.sequence, []).append(approval)

    for seq in sorted(by_seq):
        group = by_seq[seq]
        pending = [a for a in group if a.status == ApprovalStatus.PENDING.value]
        if any(a.status == ApprovalStatus.REJECTED.value for a in group):
            return []
        if pending:
            return pending
    return []


def decide(
    session: Session,
    *,
    principal,
    approval_id: str,
    approved: bool,
    comment: str = "",
    now: datetime | None = None,
) -> Approval:
    now = now or utcnow()
    approval = session.execute(
        select(Approval).where(
            Approval.id == approval_id, Approval.tenant_id == principal.tenant_id
        )
    ).scalar_one_or_none()
    if approval is None:
        raise NotFoundError(f"approval '{approval_id}' not found")

    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError(
            f"approval is already {approval.status}",
            details={"reason": "approval_not_pending", "status": approval.status},
        )
    if approval.expires_at is not None and approval.expires_at <= now:
        approval.status = ApprovalStatus.EXPIRED.value
        session.flush()
        raise ConflictError(
            "approval expired before it was decided",
            details={"reason": "approval_expired"},
        )

    required_role = Role(approval.required_role)
    if not can_decide(principal, required_role, approval.project_id):
        raise PermissionDenied(
            f"role '{required_role.value}' is required to decide this approval",
            details={"reason": "wrong_approver_role", "required_role": required_role.value},
        )
    if approval.decided_by == principal.user_id:
        raise ConflictError("already decided by this user", details={"reason": "duplicate_decision"})

    siblings = list_for_subject(
        session,
        tenant_id=approval.tenant_id,
        subject_type=approval.subject_type,
        subject_id=approval.subject_id,
    )
    if approval not in actionable(siblings):
        raise ConflictError(
            "an earlier approval in the sequence is still pending",
            details={"reason": "sequence_not_reached"},
        )

    # Dual-control: the two signatures on a production deploy must come from two
    # different people even if one person holds both roles (spec §2.1).
    same_subject_decisions = {
        a.decided_by
        for a in siblings
        if a.status == ApprovalStatus.APPROVED.value and a.decided_by
    }
    if approved and principal.user_id in same_subject_decisions:
        raise ConflictError(
            "dual control requires two distinct approvers",
            details={"reason": "dual_control_violation"},
        )

    approval.status = (
        ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
    )
    approval.decided_by = principal.user_id
    approval.decided_at = now
    approval.comment = comment
    session.flush()

    audit.record(
        session,
        tenant_id=approval.tenant_id,
        action="approval.decided",
        actor_id=principal.user_id,
        resource_type=approval.subject_type,
        resource_id=approval.subject_id,
        decision="allow" if approved else "deny",
        payload={"approval_id": approval.id, "comment": comment},
    )
    events.emit(
        session,
        tenant_id=approval.tenant_id,
        project_id=approval.project_id,
        event_type=events.APPROVAL_DECIDED,
        payload={
            "approval_id": approval.id,
            "subject_type": approval.subject_type,
            "subject_id": approval.subject_id,
            "approved": approved,
        },
    )
    return approval


def is_satisfied(
    session: Session,
    *,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    expected_checksum: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """`(satisfied, reason)` — the gate function used before a guarded action."""
    now = now or utcnow()
    approvals = [
        a
        for a in list_for_subject(
            session, tenant_id=tenant_id, subject_type=subject_type, subject_id=subject_id
        )
        if a.status != ApprovalStatus.STALE.value
    ]
    if not approvals:
        return False, "no_approvals_requested"

    for approval in approvals:
        if approval.status == ApprovalStatus.REJECTED.value:
            return False, "rejected"
        if approval.status == ApprovalStatus.PENDING.value:
            if approval.expires_at is not None and approval.expires_at <= now:
                approval.status = ApprovalStatus.EXPIRED.value
                session.flush()
                return False, "expired"
            return False, "pending"
        if approval.status == ApprovalStatus.EXPIRED.value:
            return False, "expired"
        if not approval.is_effective(now):
            return False, "expired"
        if expected_checksum and approval.subject_checksum != expected_checksum:
            return False, "stale_subject"
    return True, "satisfied"


def invalidate_for_subject(
    session: Session, *, tenant_id: str, subject_type: str, subject_id: str, reason: str
) -> int:
    """Mark all approvals for a subject stale — used when the brief changes."""
    approvals = list_for_subject(
        session, tenant_id=tenant_id, subject_type=subject_type, subject_id=subject_id
    )
    changed = 0
    for approval in approvals:
        if approval.status in {ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value}:
            approval.status = ApprovalStatus.STALE.value
            changed += 1
    if changed:
        session.flush()
        audit.record(
            session,
            tenant_id=tenant_id,
            action="approval.invalidated",
            actor_type="system",
            resource_type=subject_type,
            resource_id=subject_id,
            payload={"reason": reason, "count": changed},
        )
    return changed
