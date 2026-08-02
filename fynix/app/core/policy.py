"""Policy engine: tenant-aware RBAC + ABAC, deny by default (spec §10.2).

Every consequential call goes through `authorize()`. The engine is pure — it
takes a principal, an action and a resource description, and returns a decision
with a machine-readable reason so the audit log records *why* something was
allowed or refused, not just that it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.errors import PermissionDenied
from app.models.base import DataClass, Role

POLICY_VERSION = "v1"

# --------------------------------------------------------------------------
# Permission vocabulary
# --------------------------------------------------------------------------

# Actions that always require a second factor, regardless of role (NFR-007).
MFA_REQUIRED_ACTIONS = frozenset(
    {
        "deployment:approve_prod",
        "budget:override",
        "waiver:issue",
        "tenant:manage",
        "user:grant_role",
        "secret:rotate",
        "data:export",
        "data:delete",
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.PLATFORM_ADMIN: frozenset({"*"}),
    Role.CLIENT_OWNER: frozenset(
        {
            "project:read",
            "project:create",
            "brief:read",
            "brief:write",
            "blueprint:read",
            "blueprint:approve",
            "contract:approve",
            "budget:approve",
            "release:read",
            "deployment:approve_prod",
            "cost:read",
            "handover:receive",
            "incident:read",
        }
    ),
    Role.PROJECT_MANAGER: frozenset(
        {
            "project:read",
            "project:create",
            "project:update",
            "brief:read",
            "brief:write",
            "blueprint:read",
            "blueprint:request",
            "task:read",
            "task:assign",
            "task:create",
            "run:read",
            "run:start",
            "approval:request",
            "release:read",
            "cost:read",
            "incident:read",
            "incident:open",
            "notification:send",
        }
    ),
    Role.SOLUTION_ARCHITECT: frozenset(
        {
            "project:read",
            "brief:read",
            "blueprint:read",
            "blueprint:request",
            "blueprint:approve",
            "adr:write",
            "task:read",
            "task:create",
            "run:read",
            "run:start",
            "release:read",
            "gate:read",
        }
    ),
    Role.AI_OPERATOR: frozenset(
        {
            "project:read",
            "run:read",
            "run:start",
            "run:cancel",
            "run:retry",
            "prompt:read",
            "prompt:write",
            "model_profile:write",
            "task:read",
            "cost:read",
        }
    ),
    Role.DEVELOPER: frozenset(
        {
            "project:read",
            "task:read",
            "task:update",
            "run:read",
            "run:start",
            "changeset:read",
            "changeset:review",
            "changeset:merge",
            "gate:read",
            "release:read",
        }
    ),
    Role.QA_ENGINEER: frozenset(
        {
            "project:read",
            "task:read",
            "run:read",
            "gate:read",
            "gate:run",
            "release:read",
            "release:verdict",
            "changeset:read",
        }
    ),
    Role.DEVOPS_SRE: frozenset(
        {
            "project:read",
            "environment:read",
            "environment:write",
            "release:read",
            "release:create",
            "deployment:read",
            "deployment:create",
            "deployment:approve_prod",
            "deployment:rollback",
            "incident:read",
            "incident:open",
            "incident:resolve",
            "gate:read",
            "secret:rotate",
        }
    ),
    Role.SECURITY_COMPLIANCE: frozenset(
        {
            "project:read",
            "gate:read",
            "gate:run",
            "waiver:issue",
            "waiver:revoke",
            "dependency:approve",
            "license:write",
            "license:read",
            "audit:read",
            "run:read",
            "data:export",
            "data:delete",
            "changeset:read",
        }
    ),
    Role.FINANCE_ADMIN: frozenset(
        {
            "project:read",
            "cost:read",
            "budget:write",
            "budget:approve",
            "budget:override",
            "license:read",
            "license:write",
            "tariff:write",
        }
    ),
    Role.SERVICE_ACCOUNT: frozenset(
        {
            "project:read",
            "brief:write",
            "run:read",
            "task:read",
            "cost:read",
        }
    ),
}


@dataclass(frozen=True)
class Resource:
    """What is being acted on. Attributes feed the ABAC conditions."""

    type: str
    id: str = ""
    tenant_id: str = ""
    project_id: str | None = None
    data_class: DataClass = DataClass.INTERNAL
    environment: str = ""
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION

    def raise_if_denied(self, action: str) -> None:
        if not self.allowed:
            raise PermissionDenied(
                f"action '{action}' denied: {self.reason}",
                details={"reason": self.reason, "policy_version": self.policy_version},
            )


def _has_permission(roles: frozenset[Role], action: str) -> bool:
    for role in roles:
        perms = ROLE_PERMISSIONS.get(role, frozenset())
        if "*" in perms or action in perms:
            return True
    return False


def evaluate(principal, action: str, resource: Resource) -> Decision:
    """Pure decision function. `principal` is `app.core.security.Principal`."""
    # 1. Tenant isolation comes first — it is not overridable by any role.
    if resource.tenant_id and resource.tenant_id != principal.tenant_id:
        return Decision(False, "cross_tenant_access")

    roles = principal.roles_for(resource.project_id)
    if not roles:
        return Decision(False, "no_role_binding")

    # 2. RBAC.
    if not _has_permission(roles, action):
        return Decision(False, "missing_permission")

    # 3. ABAC conditions layered on top of the role grant.
    if action in MFA_REQUIRED_ACTIONS and not principal.mfa:
        return Decision(False, "mfa_required")

    # Service accounts never inherit privileged human actions even if a role
    # binding was granted by mistake.
    if (
        principal.is_service_account
        and action not in ROLE_PERMISSIONS[Role.SERVICE_ACCOUNT]
        and Role.PLATFORM_ADMIN not in roles
    ):
        return Decision(False, "service_account_scope")

    if (
        action == "deployment:create"
        and resource.environment == "prod"
        and not (Role.DEVOPS_SRE in roles or Role.PLATFORM_ADMIN in roles)
    ):
        return Decision(False, "prod_deploy_requires_sre")

    # Restricted data (personal data, secrets, prod dumps) is never readable by
    # a broad project role — only security/compliance and the data owner.
    if resource.data_class == DataClass.RESTRICTED and action.endswith(":read"):
        allowed = {Role.SECURITY_COMPLIANCE, Role.PLATFORM_ADMIN, Role.CLIENT_OWNER}
        if not (roles & allowed):
            return Decision(False, "restricted_data_class")

    return Decision(True, "allow")


def authorize(principal, action: str, resource: Resource) -> Decision:
    """Evaluate and raise `PermissionDenied` on refusal."""
    decision = evaluate(principal, action, resource)
    decision.raise_if_denied(action)
    return decision


# --------------------------------------------------------------------------
# Approval matrix — spec §2.1
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequirement:
    subject_type: str
    required_roles: tuple[Role, ...]
    rule: str
    # Approvals sharing a sequence number are collected in parallel.
    sequences: tuple[int, ...] = ()
    ttl_hours: int = 168

    def as_pairs(self) -> list[tuple[Role, int]]:
        seqs = self.sequences or tuple(0 for _ in self.required_roles)
        return list(zip(self.required_roles, seqs, strict=True))


APPROVAL_MATRIX: dict[str, ApprovalRequirement] = {
    "blueprint": ApprovalRequirement(
        subject_type="blueprint",
        required_roles=(Role.SOLUTION_ARCHITECT, Role.CLIENT_OWNER),
        sequences=(0, 1),
        rule="before any production code is generated",
    ),
    "budget_change": ApprovalRequirement(
        subject_type="budget_change",
        required_roles=(Role.FINANCE_ADMIN, Role.CLIENT_OWNER),
        sequences=(0, 0),
        rule="pipeline is paused until decided",
        ttl_hours=72,
    ),
    "high_risk_dependency": ApprovalRequirement(
        subject_type="high_risk_dependency",
        required_roles=(Role.SECURITY_COMPLIANCE,),
        rule="waiver or replacement required",
        ttl_hours=72,
    ),
    "deploy_prod": ApprovalRequirement(
        subject_type="deploy_prod",
        required_roles=(Role.CLIENT_OWNER, Role.DEVOPS_SRE),
        sequences=(0, 0),
        rule="dual confirmation",
        ttl_hours=24,
    ),
    "sensitive_data_access": ApprovalRequirement(
        subject_type="sensitive_data_access",
        required_roles=(Role.CLIENT_OWNER, Role.SECURITY_COMPLIANCE),
        sequences=(0, 0),
        rule="time-bound, least privilege",
        ttl_hours=8,
    ),
    "contract": ApprovalRequirement(
        subject_type="contract",
        required_roles=(Role.CLIENT_OWNER, Role.FINANCE_ADMIN),
        sequences=(0, 0),
        rule="scope and budget accepted",
    ),
}


#: Approving one of these is itself a privileged action, so the approver needs a
#: second factor (NFR-007). Without this, `approvals.decide` would only check
#: the role and a stolen session could sign off a production deploy.
APPROVAL_MFA_REQUIRED = frozenset({"deploy_prod", "budget_change", "sensitive_data_access"})


def approval_requires_mfa(subject_type: str) -> bool:
    return subject_type in APPROVAL_MFA_REQUIRED


def approval_requirement(subject_type: str) -> ApprovalRequirement:
    try:
        return APPROVAL_MATRIX[subject_type]
    except KeyError as exc:
        raise PermissionDenied(
            f"no approval policy defined for '{subject_type}'",
            details={"reason": "undefined_approval_subject"},
        ) from exc


def can_decide(principal, requirement_role: Role, project_id: str | None) -> bool:
    roles = principal.roles_for(project_id)
    return requirement_role in roles or Role.PLATFORM_ADMIN in roles


# --------------------------------------------------------------------------
# Tool grants for agent runs — spec §5.4
# --------------------------------------------------------------------------

#: Least-privilege default tool allowlist per agent type. An agent may never
#: receive a tool that is not listed here, even if a caller asks for it.
AGENT_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    "brief_analyst": ("read_brief",),
    "solution_architect": ("read_brief", "read_knowledge"),
    "estimator": ("read_blueprint", "read_metrics"),
    "planner": ("read_blueprint",),
    "code": ("read_repo", "write_workspace", "run_tests"),
    "test": ("read_repo", "write_workspace", "run_tests"),
    "review": ("read_diff", "read_standards"),
    "security": ("read_diff", "read_manifests", "read_policies"),
    "release": ("read_evidence", "read_repo"),
    "documentation": ("read_repo", "read_blueprint", "write_workspace"),
}

#: Tools that mutate anything outside the sandbox are never auto-granted.
FORBIDDEN_TOOLS = frozenset(
    {"push_protected_branch", "read_secret_value", "deploy_prod", "delete_infrastructure"}
)


def resolve_tool_grants(agent_type: str, requested: list[str] | None = None) -> list[str]:
    allowed = set(AGENT_TOOL_GRANTS.get(agent_type, ()))
    if requested is None:
        return sorted(allowed)
    asked = set(requested)
    if asked & FORBIDDEN_TOOLS:
        raise PermissionDenied(
            "requested tool is never grantable to an agent",
            details={"reason": "forbidden_tool", "tools": sorted(asked & FORBIDDEN_TOOLS)},
        )
    return sorted(asked & allowed)


def approval_expiry(requirement: ApprovalRequirement, now: datetime):
    from datetime import timedelta

    return now + timedelta(hours=requirement.ttl_hours)
