"""Request/response models for the public API (spec §9.1)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

# --- auth -----------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    tenant_id: str
    roles: list[str]


# --- projects -------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    golden_path: str = ""
    owner_user_id: str | None = None
    budget_rub: float = Field(default=0.0, ge=0)
    data_class: str = "confidential"
    source: str = "portal"


class ProjectOut(BaseModel):
    id: str
    name: str
    state: str
    golden_path: str
    owner_user_id: str | None
    data_class: str
    budget_rub: float
    repository_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectDetail(ProjectOut):
    gate: dict
    progress: dict
    budget: dict


class StateTransitionRequest(BaseModel):
    target: str
    reason: str = ""
    force: bool = False


# --- briefs / blueprints --------------------------------------------------


class BriefCreate(BaseModel):
    raw_text: str = Field(min_length=1)
    answers: dict[str, str] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)


class BriefOut(BaseModel):
    id: str
    version: int
    completeness: int
    open_questions: list
    risks: list
    checksum: str
    created_at: datetime

    model_config = {"from_attributes": True}


class BlueprintOut(BaseModel):
    id: str
    version: int
    summary: str
    architecture: dict
    backlog: list
    nfr: list
    risks: list
    estimate: dict
    is_stale: bool
    checksum: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- operations -----------------------------------------------------------


class OperationOut(BaseModel):
    id: str
    kind: str
    status: str
    job_id: str | None
    result: dict
    error: str
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


# --- approvals ------------------------------------------------------------


class ApprovalOut(BaseModel):
    id: str
    project_id: str
    subject_type: str
    subject_id: str
    required_role: str
    sequence: int
    status: str
    decided_by: str | None
    decided_at: datetime | None
    comment: str
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    approved: bool
    comment: str = ""


class WaiverCreate(BaseModel):
    project_id: str
    gate: str
    justification: str = Field(min_length=10)
    finding_key: str = ""
    hours: int = Field(default=72, ge=1, le=720)


# --- tasks / runs ---------------------------------------------------------


class TaskOut(BaseModel):
    id: str
    key: str
    title: str
    status: str
    kind: str
    executor: str
    agent_type: str | None
    priority: int
    acceptance_criteria: list
    attempts: int

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: str
    agent_type: str
    status: str
    reason: str
    model_profile: dict
    tool_grants: list
    prompt_template_version: str
    prompt_checksum: str
    prompt_tokens: int
    completion_tokens: int
    cost_rub: float
    latency_ms: int
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class RunRequest(BaseModel):
    agent_type: str | None = None
    override_profile: str | None = None


# --- releases / deployments ----------------------------------------------


class ReleaseCreate(BaseModel):
    project_id: str
    version: str
    artifact_digest: str
    change_set_ids: list[str] = Field(default_factory=list)
    release_notes: str = ""
    rollback_plan: str = ""
    migration_plan: str = ""


class ReleaseOut(BaseModel):
    id: str
    version: str
    artifact_digest: str
    status: str
    release_notes: str
    rollback_plan: str
    evidence_artifact_id: str | None
    sbom_artifact_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeploymentCreate(BaseModel):
    project_id: str
    release_id: str
    environment: str
    strategy: str = "rolling"


class DeploymentOut(BaseModel):
    id: str
    environment_id: str
    release_id: str
    strategy: str
    status: str
    requested_by: str
    health: dict
    reason: str
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class RollbackRequest(BaseModel):
    reason: str = Field(min_length=3)


class HealthReport(BaseModel):
    healthy: bool
    reason: str = ""
    checks: dict = Field(default_factory=dict)


# --- cost / budget --------------------------------------------------------


class BudgetOverrideRequest(BaseModel):
    amount: float = Field(gt=0)
    hours: int = Field(default=24, ge=1, le=720)
    reason: str = Field(min_length=10)


# --- webhooks -------------------------------------------------------------


class WebhookCreate(BaseModel):
    url: str
    event_types: list[str] = Field(default_factory=list)
    project_id: str | None = None


class WebhookOut(BaseModel):
    id: str
    url: str
    event_types: list
    project_id: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class WebhookCreated(WebhookOut):
    secret: str


# --- admin ----------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,60}$")
    plan: str = "pilot"
    max_external_data_class: str = "internal"
    monthly_budget_rub: float = 0.0


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = ""
    password: str = Field(min_length=12)
    roles: list[str] = Field(default_factory=list)
    mfa_enabled: bool = False


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    mfa_enabled: bool
    is_service_account: bool

    model_config = {"from_attributes": True}


class RoleGrant(BaseModel):
    user_id: str
    role: str
    project_id: str | None = None
    expires_at: datetime | None = None
