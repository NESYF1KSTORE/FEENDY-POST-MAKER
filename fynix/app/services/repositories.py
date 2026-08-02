"""Repository adapter (spec §4.2 "Repository Adapter", FR-007/FR-011).

The port exposes what the workflow needs — create a repo, branch, commit a
change set, open a pull request — and refuses a direct push to a protected
branch. The MVP driver is a local bare Git repository on the same host, which is
enough for a single-VPS install; a GitHub/GitLab driver implements the same
functions without touching the orchestrator.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import audit
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.core.logging import get_logger
from app.models.base import utcnow
from app.models.delivery import ChangeSet, Repository
from app.runners.manager import Workspace

log = get_logger("fynix.repo")


def _git(args: list[str], cwd: Path | str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise ValidationError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}",
            details={"reason": "git_error", "args": args},
        )
    return proc


def repo_root() -> Path:
    root = Path(get_settings().artifact_dir).parent / "repos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def create(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    name: str,
    template: str = "",
    default_branch: str = "main",
    code_owners: list[str] | None = None,
    actor_id: str = "",
) -> Repository:
    """Create the project repository with branch protection already on (FR-007)."""
    existing = session.execute(
        select(Repository).where(
            Repository.tenant_id == tenant_id, Repository.project_id == project_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    full_name = f"{tenant_id}/{name}"
    path = repo_root() / tenant_id / f"{name}.git"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "--initial-branch", default_branch, str(path)], cwd=path.parent)

    repository = Repository(
        tenant_id=tenant_id,
        project_id=project_id,
        provider="local",
        full_name=full_name,
        default_branch=default_branch,
        protected_branches=[default_branch],
        code_owners=code_owners or [],
        required_checks=["G1", "G2", "G5"],
        template=template,
        clone_url=f"file://{path}",
    )
    session.add(repository)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="repository.created",
        actor_id=actor_id,
        resource_type="repository",
        resource_id=repository.id,
        payload={"full_name": full_name, "protected": repository.protected_branches},
    )
    return repository


def get_for_project(session: Session, *, tenant_id: str, project_id: str) -> Repository:
    repository = session.execute(
        select(Repository).where(
            Repository.tenant_id == tenant_id, Repository.project_id == project_id
        )
    ).scalar_one_or_none()
    if repository is None:
        raise NotFoundError("project has no repository yet")
    return repository


def assert_branch_writable(repository: Repository, branch: str) -> None:
    """FR-011: an agent may never write straight to a protected branch."""
    if branch in (repository.protected_branches or []):
        raise PermissionDenied(
            f"direct writes to protected branch '{branch}' are not allowed; open a pull request",
            details={"reason": "protected_branch", "branch": branch},
        )


@dataclass
class CommitResult:
    branch: str
    commit_sha: str
    files_changed: list[str]
    diff: str


def checkout(workspace: Workspace, repository: Repository, branch: str) -> None:
    """Clone the repo into the sandbox and switch to a feature branch."""
    clone_path = Path(repository.clone_url.removeprefix("file://"))
    if not clone_path.exists():
        raise NotFoundError(f"repository storage missing at {clone_path}")

    _git(["clone", str(clone_path), "."], cwd=workspace.path)
    _git(["config", "user.email", "agent@fynix.local"], cwd=workspace.path)
    _git(["config", "user.name", "FYNIX Agent"], cwd=workspace.path)

    # A brand-new bare repo has no commits, so the default branch has no ref yet.
    has_head = _git(["rev-parse", "--verify", "HEAD"], cwd=workspace.path, check=False)
    if has_head.returncode == 0:
        _git(["checkout", "-B", branch], cwd=workspace.path)
    else:
        _git(["checkout", "-b", branch], cwd=workspace.path)


def commit_changes(
    workspace: Workspace,
    repository: Repository,
    *,
    branch: str,
    files: list[dict],
    message: str,
) -> CommitResult:
    """Apply a change set inside the sandbox and push it to a feature branch."""
    assert_branch_writable(repository, branch)

    touched = workspace.apply_changes(files)
    _git(["add", "-A"], cwd=workspace.path)

    status = _git(["status", "--porcelain"], cwd=workspace.path)
    if not status.stdout.strip():
        raise ValidationError("change set produced no modifications")

    _git(["commit", "-m", message], cwd=workspace.path)
    sha = _git(["rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()
    diff = _git(["show", "--stat", "--patch", "HEAD"], cwd=workspace.path).stdout
    _git(["push", "origin", branch], cwd=workspace.path)

    return CommitResult(branch=branch, commit_sha=sha, files_changed=touched, diff=diff[:200_000])


def open_pull_request(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    repository: Repository,
    task_id: str | None,
    run_id: str | None,
    commit: CommitResult,
    title: str,
) -> ChangeSet:
    """Record the PR. Merging still requires the required checks to pass."""
    change_set = ChangeSet(
        tenant_id=tenant_id,
        project_id=project_id,
        repository_id=repository.id,
        task_id=task_id,
        run_id=run_id,
        branch=commit.branch,
        base_branch=repository.default_branch,
        title=title,
        diff=commit.diff,
        files_changed=commit.files_changed,
        commit_sha=commit.commit_sha,
        pr_url=f"{repository.clone_url}#{commit.branch}",
        status="open",
    )
    session.add(change_set)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="changeset.pr_opened",
        actor_type="agent",
        resource_type="change_set",
        resource_id=change_set.id,
        payload={
            "branch": commit.branch,
            "commit": commit.commit_sha,
            "files": len(commit.files_changed),
            "run_id": run_id,
        },
    )
    return change_set


def merge(
    session: Session,
    *,
    change_set: ChangeSet,
    repository: Repository,
    passed_checks: list[str],
    actor_id: str,
) -> ChangeSet:
    """Merge a PR only when every required check has passed (FR-012)."""
    missing = [c for c in (repository.required_checks or []) if c not in passed_checks]
    if missing:
        raise PermissionDenied(
            "required checks have not passed for this change set",
            details={"reason": "required_checks_missing", "missing": missing},
        )

    clone_path = Path(repository.clone_url.removeprefix("file://"))
    # Fast-forward the protected branch inside the bare repo; the agent never
    # performs this step — only the platform does, after the checks.
    _git(
        ["update-ref", f"refs/heads/{repository.default_branch}", change_set.commit_sha],
        cwd=clone_path,
    )

    change_set.status = "merged"
    change_set.merged_at = utcnow()
    session.flush()

    audit.record(
        session,
        tenant_id=change_set.tenant_id,
        action="changeset.merged",
        actor_id=actor_id,
        resource_type="change_set",
        resource_id=change_set.id,
        payload={"commit": change_set.commit_sha, "checks": passed_checks},
    )
    return change_set
