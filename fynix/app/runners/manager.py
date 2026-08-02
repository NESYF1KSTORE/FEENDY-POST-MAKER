"""Ephemeral runner workspaces (spec §10.2 "Runner isolation", AC-11).

Two drivers:
  * `local`   — a throwaway directory; used in CI and on a single-VPS install.
  * `docker`  — a one-shot container with no network, a read-only root, a
                non-root user, dropped capabilities, and CPU/memory/pid caps.

Every workspace is registered in the database with a TTL so the reconciler can
find and destroy orphans left by a crashed worker.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.models.base import utcnow
from app.models.execution import RunnerWorkspace

log = get_logger("fynix.runner")


@dataclass
class SandboxProfile:
    """Isolation settings applied to a run. Defaults are the strict ones."""

    network: str = "none"          # none | egress-allowlist
    read_only_root: bool = True
    user: str = "10001:10001"      # never root inside the sandbox
    cpu: str = "1"
    memory: str = "1g"
    pids_limit: int = 256
    timeout_s: int = 900
    allowed_egress: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "network": self.network,
            "read_only_root": self.read_only_root,
            "user": self.user,
            "cpu": self.cpu,
            "memory": self.memory,
            "pids_limit": self.pids_limit,
            "timeout_s": self.timeout_s,
            "allowed_egress": list(self.allowed_egress),
        }


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Workspace:
    """A checked-out working directory plus the record that tracks its lifetime."""

    def __init__(self, record: RunnerWorkspace, path: Path, profile: SandboxProfile) -> None:
        self.record = record
        self.path = path
        self.profile = profile

    # -- file helpers ----------------------------------------------------

    def write_file(self, relative_path: str, content: str) -> Path:
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def delete_file(self, relative_path: str) -> None:
        target = self._resolve(relative_path)
        if target.exists():
            target.unlink()

    def _resolve(self, relative_path: str) -> Path:
        """Reject path traversal — a workspace never writes outside itself."""
        if relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise ValidationError(
                f"path '{relative_path}' escapes the workspace",
                details={"reason": "path_traversal"},
            )
        resolved = (self.path / relative_path).resolve()
        if not str(resolved).startswith(str(self.path.resolve())):
            raise ValidationError(
                f"path '{relative_path}' escapes the workspace",
                details={"reason": "path_traversal"},
            )
        return resolved

    def apply_changes(self, files: list[dict]) -> list[str]:
        """Apply a code agent's file list. Returns the paths touched."""
        touched = []
        for entry in files:
            path = entry.get("path", "")
            action = entry.get("action", "update")
            if not path:
                continue
            if action == "delete":
                self.delete_file(path)
            else:
                self.write_file(path, entry.get("content", ""))
            touched.append(path)
        return touched

    # -- execution -------------------------------------------------------

    def run(self, command: list[str], *, timeout_s: int | None = None) -> ExecResult:
        driver = self.record.driver
        timeout = timeout_s or self.profile.timeout_s
        if driver == "docker":
            return self._run_docker(command, timeout)
        return self._run_local(command, timeout)

    def _run_local(self, command: list[str], timeout: int) -> ExecResult:
        try:
            proc = subprocess.run(
                command,
                cwd=self.path,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, exc.stdout or "", exc.stderr or "", timed_out=True)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)

    def _run_docker(self, command: list[str], timeout: int) -> ExecResult:
        settings = get_settings()
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", self.profile.network,
            "--user", self.profile.user,
            "--cpus", self.profile.cpu,
            "--memory", self.profile.memory,
            "--pids-limit", str(self.profile.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # The workspace is the only writable mount; the image root is not.
            "--mount", f"type=bind,source={self.path},target=/workspace",
            "--workdir", "/workspace",
        ]
        if self.profile.read_only_root:
            docker_cmd += ["--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
        docker_cmd += [settings.runner_image, *command]

        try:
            proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=timeout + 30, check=False
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, exc.stdout or "", exc.stderr or "", timed_out=True)
        except FileNotFoundError:
            log.warning("runner.docker_unavailable", falling_back_to="local")
            return self._run_local(command, timeout)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)


def acquire(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    run_id: str | None = None,
    profile: SandboxProfile | None = None,
) -> Workspace:
    """Create a fresh workspace. Never reuse one across runs (spec §10.2)."""
    settings = get_settings()
    profile = profile or SandboxProfile(
        cpu=settings.runner_cpu,
        memory=settings.runner_memory,
        timeout_s=settings.runner_timeout_s,
    )

    root = Path(settings.runner_workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="ws-", dir=str(root)))

    record = RunnerWorkspace(
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        driver=settings.runner_driver,
        path=str(path),
        profile=profile.as_dict(),
        expires_at=utcnow() + timedelta(seconds=profile.timeout_s * 2),
    )
    session.add(record)
    session.flush()
    log.info(
        "runner.workspace_acquired",
        workspace_id=record.id,
        driver=record.driver,
        project_id=project_id,
        run_id=run_id,
    )
    return Workspace(record, path, profile)


def release(session: Session, workspace: Workspace) -> None:
    """Destroy the workspace directory and close the record."""
    shutil.rmtree(workspace.path, ignore_errors=True)
    workspace.record.released_at = utcnow()
    session.flush()
    log.info("runner.workspace_released", workspace_id=workspace.record.id)


def reconcile_orphans(session: Session) -> int:
    """Delete workspaces whose TTL expired without a release (AC-11)."""
    now = utcnow()
    orphans = list(
        session.execute(
            select(RunnerWorkspace).where(
                RunnerWorkspace.released_at.is_(None), RunnerWorkspace.expires_at <= now
            )
        )
        .scalars()
        .all()
    )
    for record in orphans:
        if record.path:
            shutil.rmtree(record.path, ignore_errors=True)
        record.released_at = now
        log.warning(
            "runner.orphan_reclaimed", workspace_id=record.id, run_id=record.run_id
        )
    session.flush()
    return len(orphans)
