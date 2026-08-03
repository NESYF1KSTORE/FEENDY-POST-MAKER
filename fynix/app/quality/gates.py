"""Quality gates G0..G7 (spec §11.1).

Each gate returns a `QualityGateResult` row with a status, findings and evidence.
A blocking gate that fails stops the pipeline; a waiver issued by
security/compliance can unblock it, but only until the waiver expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.models.base import GateStatus, utcnow
from app.models.delivery import QualityGateResult
from app.models.governance import PolicyWaiver
from app.orchestrator import events
from app.quality import scanners

#: Gate id -> (title, blocking, evidence TTL). The TTL implements "gate evidence
#: has a validity period" from spec §3.1.
GATES: dict[str, tuple[str, bool, timedelta]] = {
    "G0": ("Requirements", True, timedelta(days=30)),
    "G1": ("Static", True, timedelta(days=7)),
    "G2": ("Unit", True, timedelta(days=7)),
    "G3": ("Integration", True, timedelta(days=7)),
    "G4": ("E2E", True, timedelta(days=7)),
    "G5": ("Security", True, timedelta(days=7)),
    "G6": ("Performance", False, timedelta(days=14)),
    "G7": ("Release", True, timedelta(days=3)),
}

BLOCKING_SEVERITIES = frozenset({"critical", "high"})


@dataclass
class GateOutcome:
    gate: str
    status: GateStatus
    findings: list[dict]
    evidence: dict
    score: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status in {GateStatus.PASSED, GateStatus.WAIVED, GateStatus.SKIPPED}


# --------------------------------------------------------------------------
# Individual gate implementations
# --------------------------------------------------------------------------


def gate_g0_requirements(task_specs: list[dict]) -> GateOutcome:
    """Acceptance criteria must exist, be testable and have an owner."""
    findings = []
    vague = ("хорошо", "быстро", "удобно", "качественно", "красиво", "оптимально")
    for spec in task_specs:
        key = spec.get("key", "?")
        criteria = spec.get("acceptance_criteria") or []
        if not criteria:
            findings.append(
                {
                    "rule": "requirements.no_acceptance_criteria",
                    "severity": "critical",
                    "file": key,
                    "detail": f"у задачи '{key}' нет критериев приёмки",
                }
            )
            continue
        for criterion in criteria:
            if any(word in str(criterion).lower() for word in vague):
                findings.append(
                    {
                        "rule": "requirements.vague_criterion",
                        "severity": "medium",
                        "file": key,
                        "detail": f"неизмеримая формулировка: «{criterion}»",
                    }
                )
        if not spec.get("executor"):
            findings.append(
                {
                    "rule": "requirements.no_owner",
                    "severity": "high",
                    "file": key,
                    "detail": f"у задачи '{key}' не назначен исполнитель",
                }
            )

    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    return GateOutcome(
        gate="G0",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={"tasks_checked": len(task_specs)},
        score=100.0 - len(findings) * 5,
    )


def gate_g1_static(files: list[dict]) -> GateOutcome:
    """Formatting, obvious static defects and — critically — secret scanning."""
    findings = [f.as_dict() for f in scanners.scan_secrets(files)]
    findings += [f.as_dict() for f in scanners.scan_static(files)]
    for entry in files:
        content = entry.get("content") or ""
        if content and not content.endswith("\n") and entry.get("action") != "delete":
            findings.append(
                {
                    "rule": "style.no_trailing_newline",
                    "severity": "info",
                    "file": entry.get("path", ""),
                    "detail": "файл не заканчивается переводом строки",
                }
            )
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    return GateOutcome(
        gate="G1",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={"files_scanned": len(files), "blocking": len(blocking)},
        score=max(0.0, 100.0 - len(blocking) * 25 - len(findings)),
    )


def gate_g2_unit(test_report: dict) -> GateOutcome:
    """Unit tests: failures block; coverage below policy blocks."""
    passed = int(test_report.get("passed", 0))
    failed = int(test_report.get("failed", 0))
    coverage = float(test_report.get("coverage", 0.0))
    minimum = float(test_report.get("min_coverage", 60.0))

    findings = []
    if failed:
        findings.append(
            {
                "rule": "tests.failed",
                "severity": "critical",
                "file": "test-suite",
                "detail": f"{failed} тест(ов) не прошли",
            }
        )
    if passed + failed == 0:
        findings.append(
            {
                "rule": "tests.none",
                "severity": "high",
                "file": "test-suite",
                "detail": "тесты не запускались",
            }
        )
    elif coverage < minimum:
        findings.append(
            {
                "rule": "tests.coverage_below_policy",
                "severity": "high",
                "file": "test-suite",
                "detail": f"покрытие {coverage:.1f}% ниже порога {minimum:.1f}%",
            }
        )
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    return GateOutcome(
        gate="G2",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={"passed": passed, "failed": failed, "coverage": coverage},
        score=coverage,
    )


def gate_g3_integration(report: dict) -> GateOutcome:
    findings = []
    for failure in report.get("contract_failures", []):
        findings.append(
            {
                "rule": "integration.contract_failed",
                "severity": "critical",
                "file": str(failure.get("target", "unknown")),
                "detail": str(failure.get("detail", "контракт не выполнен")),
            }
        )
    if report.get("migration_failed"):
        findings.append(
            {
                "rule": "integration.migration_failed",
                "severity": "critical",
                "file": "migrations",
                "detail": "миграция не применилась",
            }
        )
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    return GateOutcome(
        gate="G3",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence=report,
    )


def gate_g4_e2e(report: dict) -> GateOutcome:
    findings = []
    for scenario in report.get("scenarios", []):
        if not scenario.get("passed") and scenario.get("critical", True):
            findings.append(
                {
                    "rule": "e2e.critical_scenario_failed",
                    "severity": "critical",
                    "file": str(scenario.get("name", "scenario")),
                    "detail": str(scenario.get("detail", "сценарий не пройден")),
                }
            )
    blocking = bool(findings)
    return GateOutcome(
        gate="G4",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={"scenarios": len(report.get("scenarios", []))},
    )


def gate_g5_security(
    files: list[dict], agent_findings: list[dict] | None = None, known_licenses: dict | None = None
) -> GateOutcome:
    """Secret scan + SAST + dependency/licence policy + the security agent's verdict."""
    findings = [f.as_dict() for f in scanners.scan_secrets(files)]
    findings += [f.as_dict() for f in scanners.scan_static(files)]
    findings += [f.as_dict() for f in scanners.scan_dependencies(files, known_licenses)]
    for finding in agent_findings or []:
        findings.append(
            {
                "rule": f"agent.{finding.get('category', 'other')}",
                "severity": finding.get("severity", "medium"),
                "file": finding.get("location", ""),
                "detail": finding.get("detail", ""),
                "remediation": finding.get("remediation", ""),
            }
        )
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    sbom = scanners.build_sbom(files, known_licenses)
    return GateOutcome(
        gate="G5",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={
            "blocking": len(blocking),
            "components": len(sbom.components),
            "sbom": sbom.as_dict(),
        },
        score=max(0.0, 100.0 - len(blocking) * 30),
    )


def gate_g6_performance(report: dict) -> GateOutcome:
    """Non-blocking by default; a regression beyond the budget still reports."""
    findings = []
    budget_pct = float(report.get("regression_budget_pct", 10.0))
    for metric in report.get("metrics", []):
        baseline = float(metric.get("baseline", 0) or 0)
        current = float(metric.get("current", 0) or 0)
        if baseline > 0 and current > baseline * (1 + budget_pct / 100):
            findings.append(
                {
                    "rule": "performance.regression",
                    "severity": "high",
                    "file": str(metric.get("name", "metric")),
                    "detail": f"{metric.get('name')}: {current} против базовых {baseline}",
                }
            )
    return GateOutcome(
        gate="G6",
        status=GateStatus.FAILED if findings else GateStatus.PASSED,
        findings=findings,
        evidence=report,
    )


def gate_g7_release(bundle: dict) -> GateOutcome:
    """Evidence completeness, verified rollback and valid approvals (Appendix B)."""
    required = [
        "release_manifest",
        "change_sets",
        "test_reports",
        "security_reports",
        "sbom",
        "rollback_plan",
        "approvals",
    ]
    findings = [
        {
            "rule": "release.evidence_incomplete",
            "severity": "critical",
            "file": section,
            "detail": f"в evidence bundle отсутствует раздел '{section}'",
        }
        for section in required
        if not bundle.get(section)
    ]
    if not bundle.get("rollback_verified"):
        findings.append(
            {
                "rule": "release.rollback_unverified",
                "severity": "high",
                "file": "rollback_plan",
                "detail": "план отката не проверен",
            }
        )
    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
    return GateOutcome(
        gate="G7",
        status=GateStatus.FAILED if blocking else GateStatus.PASSED,
        findings=findings,
        evidence={"sections": sorted(bundle)},
    )


# --------------------------------------------------------------------------
# Persistence + waivers
# --------------------------------------------------------------------------


def active_waiver(
    session: Session, *, tenant_id: str, project_id: str, gate: str, finding_key: str = "", now: datetime | None = None
) -> PolicyWaiver | None:
    now = now or utcnow()
    candidates = (
        session.execute(
            select(PolicyWaiver).where(
                PolicyWaiver.tenant_id == tenant_id,
                PolicyWaiver.project_id == project_id,
                PolicyWaiver.gate == gate,
            )
        )
        .scalars()
        .all()
    )
    for waiver in candidates:
        if not waiver.is_active(now):
            continue
        # An empty finding_key on the waiver covers the whole gate.
        if not waiver.finding_key or waiver.finding_key == finding_key:
            return waiver
    return None


def persist(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    subject_type: str,
    subject_id: str,
    outcome: GateOutcome,
    now: datetime | None = None,
) -> QualityGateResult:
    """Store a gate result, applying any active waiver, and emit on failure."""
    now = now or utcnow()
    title, blocking, ttl = GATES.get(outcome.gate, (outcome.gate, True, timedelta(days=7)))

    status = outcome.status
    waiver_id = None
    if status == GateStatus.FAILED:
        waiver = active_waiver(
            session, tenant_id=tenant_id, project_id=project_id, gate=outcome.gate, now=now
        )
        if waiver is not None:
            status = GateStatus.WAIVED
            waiver_id = waiver.id

    result = QualityGateResult(
        tenant_id=tenant_id,
        project_id=project_id,
        subject_type=subject_type,
        subject_id=subject_id,
        gate=outcome.gate,
        status=status.value,
        score=outcome.score,
        blocking=blocking,
        findings=outcome.findings,
        evidence={**outcome.evidence, "title": title},
        waiver_id=waiver_id,
        expires_at=now + ttl,
    )
    session.add(result)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="quality.gate_evaluated",
        actor_type="system",
        resource_type=subject_type,
        resource_id=subject_id,
        decision="allow" if status != GateStatus.FAILED else "deny",
        payload={
            "gate": outcome.gate,
            "status": status.value,
            "findings": len(outcome.findings),
            "waiver_id": waiver_id,
        },
    )
    if status == GateStatus.FAILED:
        events.emit(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            event_type=events.QUALITY_GATE_FAILED,
            payload={
                "gate": outcome.gate,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "blocking_findings": [
                    f for f in outcome.findings if f.get("severity") in BLOCKING_SEVERITIES
                ][:20],
            },
        )
    return result


def blockers(
    session: Session, *, tenant_id: str, subject_id: str, now: datetime | None = None
) -> list[QualityGateResult]:
    """Gate results that currently prevent progress (failed, blocking, or expired)."""
    now = now or utcnow()
    rows = (
        session.execute(
            select(QualityGateResult).where(
                QualityGateResult.tenant_id == tenant_id,
                QualityGateResult.subject_id == subject_id,
            )
        )
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        if not row.blocking:
            continue
        if row.status == GateStatus.FAILED.value or (row.expires_at is not None and row.expires_at <= now):
            out.append(row)
    return out
