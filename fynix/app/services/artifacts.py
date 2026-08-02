"""Immutable artifact store (spec §4.1).

Content is written to a content-addressed path under `ARTIFACT_DIR`; the row in
`artifacts` records the checksum, producer and classification. Writing the same
bytes twice yields the same path, so retries never create divergent artifacts.
The local filesystem is the MVP driver behind an S3-shaped interface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.hashing import canonical_json, sha256_hex
from app.core.tenancy import storage_prefix
from app.models.base import DataClass
from app.models.execution import Artifact


def _root() -> Path:
    path = Path(get_settings().artifact_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _content_path(tenant_id: str, project_id: str, digest_hex: str) -> Path:
    prefix = storage_prefix(tenant_id, project_id)
    # Two-level fan-out keeps directory sizes sane on the local driver.
    return _root() / prefix / digest_hex[:2] / digest_hex[2:4] / digest_hex


def put_bytes(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    name: str,
    type: str,
    data: bytes,
    data_class: DataClass = DataClass.INTERNAL,
    producer_run_id: str | None = None,
    producer_kind: str = "agent",
    meta: dict | None = None,
) -> Artifact:
    if not name:
        raise ValidationError("artifact name is required")
    digest_hex = sha256_hex(data)
    checksum = f"sha256:{digest_hex}"

    path = _content_path(tenant_id, project_id, digest_hex)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    existing = session.execute(
        select(Artifact).where(
            Artifact.tenant_id == tenant_id,
            Artifact.project_id == project_id,
            Artifact.checksum == checksum,
            Artifact.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    artifact = Artifact(
        tenant_id=tenant_id,
        project_id=project_id,
        type=type,
        name=name,
        uri=f"file://{path}",
        checksum=checksum,
        size_bytes=len(data),
        data_class=data_class.value,
        producer_run_id=producer_run_id,
        producer_kind=producer_kind,
        meta=meta or {},
    )
    session.add(artifact)
    session.flush()
    return artifact


def put_json(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    name: str,
    type: str,
    payload: dict | list,
    **kwargs,
) -> Artifact:
    return put_bytes(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        name=name,
        type=type,
        data=canonical_json(payload).encode("utf-8"),
        **kwargs,
    )


def read_bytes(artifact: Artifact) -> bytes:
    if not artifact.uri.startswith("file://"):
        raise ValidationError(f"unsupported artifact scheme: {artifact.uri}")
    path = Path(artifact.uri.removeprefix("file://"))
    if not path.exists():
        raise NotFoundError(f"artifact content missing for '{artifact.id}'")
    data = path.read_bytes()
    if f"sha256:{sha256_hex(data)}" != artifact.checksum:
        raise ValidationError(
            f"artifact '{artifact.id}' failed integrity check — content was modified"
        )
    return data


def read_json(artifact: Artifact) -> dict | list:
    return json.loads(read_bytes(artifact).decode("utf-8"))


def get(session: Session, *, tenant_id: str, artifact_id: str) -> Artifact:
    artifact = session.execute(
        select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.id == artifact_id)
    ).scalar_one_or_none()
    if artifact is None:
        raise NotFoundError(f"artifact '{artifact_id}' not found")
    return artifact


def list_for_project(
    session: Session, *, tenant_id: str, project_id: str, type: str | None = None
) -> list[Artifact]:
    stmt = select(Artifact).where(
        Artifact.tenant_id == tenant_id, Artifact.project_id == project_id
    )
    if type:
        stmt = stmt.where(Artifact.type == type)
    return list(session.execute(stmt.order_by(Artifact.created_at.desc())).scalars().all())
