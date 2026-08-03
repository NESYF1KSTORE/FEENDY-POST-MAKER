"""Evidence bundle assembly (FR-014, Appendix B).

The bundle is a single immutable artifact. Once stored, its checksum is what the
release approval is bound to — changing anything after approval invalidates the
signature rather than silently shipping different content.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.hashing import checksum
from app.models.base import DataClass, utcnow
from app.models.delivery import ChangeSet, QualityGateResult, Release
from app.models.execution import AgentRun
from app.models.governance import Approval, PolicyWaiver
from app.services import artifacts as artifacts_service


def build_bundle(
    session: Session, *, tenant_id: str, project_id: str, release: Release
) -> dict:
    """Collect every section Appendix B requires for `release`."""
    change_sets = list(
        session.execute(
            select(ChangeSet).where(
                ChangeSet.tenant_id == tenant_id,
                ChangeSet.id.in_(release.change_set_ids or []),
            )
        )
        .scalars()
        .all()
    )
    run_ids = [cs.run_id for cs in change_sets if cs.run_id]
    runs = list(
        session.execute(select(AgentRun).where(AgentRun.id.in_(run_ids))).scalars().all()
    ) if run_ids else []

    gate_rows = list(
        session.execute(
            select(QualityGateResult).where(
                QualityGateResult.tenant_id == tenant_id,
                QualityGateResult.subject_id.in_([release.id, *[cs.id for cs in change_sets]]),
            )
        )
        .scalars()
        .all()
    )
    approvals = list(
        session.execute(
            select(Approval).where(
                Approval.tenant_id == tenant_id, Approval.project_id == project_id
            )
        )
        .scalars()
        .all()
    )
    waivers = list(
        session.execute(
            select(PolicyWaiver).where(
                PolicyWaiver.tenant_id == tenant_id, PolicyWaiver.project_id == project_id
            )
        )
        .scalars()
        .all()
    )

    def _gate_section(gate_prefixes: tuple[str, ...]) -> list[dict]:
        return [
            {
                "gate": g.gate,
                "status": g.status,
                "score": g.score,
                "findings": g.findings,
                "evidence": {k: v for k, v in (g.evidence or {}).items() if k != "sbom"},
                "expires_at": g.expires_at.isoformat() if g.expires_at else None,
            }
            for g in gate_rows
            if g.gate in gate_prefixes
        ]

    sbom = next(
        (
            (g.evidence or {}).get("sbom")
            for g in gate_rows
            if g.gate == "G5" and (g.evidence or {}).get("sbom")
        ),
        None,
    )

    bundle = {
        "release_manifest": {
            "release_id": release.id,
            "version": release.version,
            "artifact_digest": release.artifact_digest,
            "project_id": project_id,
            "created_at": release.created_at.isoformat(),
        },
        "change_sets": [
            {
                "id": cs.id,
                "branch": cs.branch,
                "title": cs.title,
                "commit_sha": cs.commit_sha,
                "pr_url": cs.pr_url,
                "task_id": cs.task_id,
                "run_id": cs.run_id,
                "files_changed": cs.files_changed,
            }
            for cs in change_sets
        ],
        "provenance": [
            {
                "run_id": r.id,
                "agent_type": r.agent_type,
                "model_profile": r.model_profile,
                "prompt_template_version": r.prompt_template_version,
                "prompt_checksum": r.prompt_checksum,
                "tool_grants": r.tool_grants,
                "tokens": r.total_tokens,
                "cost_rub": r.cost_rub,
            }
            for r in runs
        ],
        "test_reports": _gate_section(("G2", "G3", "G4")),
        "security_reports": _gate_section(("G1", "G5")),
        "performance_baseline": _gate_section(("G6",)),
        "sbom": sbom or {"components": []},
        "migration_plan": release.migration_plan,
        "rollback_plan": release.rollback_plan,
        "rollback_verified": bool(release.verdict.get("rollback_verified")),
        "release_notes": release.release_notes,
        "approvals": [
            {
                "id": a.id,
                "subject_type": a.subject_type,
                "subject_id": a.subject_id,
                "required_role": a.required_role,
                "status": a.status,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                "policy_version": a.policy_version,
            }
            for a in approvals
        ],
        "waivers": [
            {
                "id": w.id,
                "gate": w.gate,
                "finding_key": w.finding_key,
                "justification": w.justification,
                "issued_by": w.issued_by,
                "expires_at": w.expires_at.isoformat(),
            }
            for w in waivers
        ],
        "generated_at": utcnow().isoformat(),
    }
    bundle["bundle_checksum"] = checksum(
        {k: v for k, v in bundle.items() if k != "generated_at"}
    )
    return bundle


def store_bundle(
    session: Session, *, tenant_id: str, project_id: str, release: Release
) -> tuple[dict, str]:
    """Build the bundle, persist it as an immutable artifact and link the release."""
    bundle = build_bundle(session, tenant_id=tenant_id, project_id=project_id, release=release)
    artifact = artifacts_service.put_json(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        name=f"evidence-{release.version}.json",
        type="evidence_bundle",
        payload=bundle,
        data_class=DataClass.CONFIDENTIAL,
        producer_kind="system",
        meta={"release_id": release.id, "version": release.version},
    )
    release.evidence_artifact_id = artifact.id

    if bundle.get("sbom", {}).get("components"):
        sbom_artifact = artifacts_service.put_json(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            name=f"sbom-{release.version}.json",
            type="sbom",
            payload=bundle["sbom"],
            data_class=DataClass.INTERNAL,
            producer_kind="system",
            meta={"release_id": release.id},
        )
        release.sbom_artifact_id = sbom_artifact.id

    session.flush()
    return bundle, artifact.id
