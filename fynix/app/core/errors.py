"""Domain errors mapped to HTTP responses by `app.main`."""

from __future__ import annotations


class FynixError(Exception):
    """Base class. `code` is stable and safe to show to API clients."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class ValidationError(FynixError):
    status_code = 422
    code = "validation_error"


class NotFoundError(FynixError):
    status_code = 404
    code = "not_found"


class ConflictError(FynixError):
    status_code = 409
    code = "conflict"


class AuthenticationError(FynixError):
    status_code = 401
    code = "unauthenticated"


class PermissionDenied(FynixError):
    """Raised by the policy engine. Deny-by-default is the norm, not an anomaly."""

    status_code = 403
    code = "permission_denied"


class PolicyConflict(FynixError):
    """Spec §5.4 — contradictory instructions stop the run and go to a human."""

    status_code = 409
    code = "policy_conflict"


class BudgetExceeded(FynixError):
    """Spec §14.1 hard stop."""

    status_code = 402
    code = "budget_exceeded"


class GateBlocked(FynixError):
    """A blocking quality/security gate refused the transition (§11.1)."""

    status_code = 409
    code = "gate_blocked"


class ApprovalRequired(FynixError):
    status_code = 409
    code = "approval_required"


class StateTransitionError(FynixError):
    status_code = 409
    code = "invalid_state_transition"


class ProviderError(FynixError):
    """Upstream AI/cloud provider failure — retryable unless `permanent`."""

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, permanent: bool = False, details: dict | None = None):
        super().__init__(message, details=details)
        self.permanent = permanent
