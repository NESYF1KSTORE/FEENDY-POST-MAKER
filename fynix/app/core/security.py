"""Password hashing, JWT issuance and API-token handling (NFR-007)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings
from app.core.errors import AuthenticationError
from app.models.base import Role

_hasher = PasswordHasher()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Passed to the policy engine on every decision."""

    user_id: str
    tenant_id: str
    email: str = ""
    roles: frozenset[Role] = field(default_factory=frozenset)
    # Roles granted only inside one project: {project_id: {roles}}
    project_roles: dict[str, frozenset[Role]] = field(default_factory=dict)
    is_service_account: bool = False
    mfa: bool = False
    token_id: str = ""

    def roles_for(self, project_id: str | None) -> frozenset[Role]:
        if project_id is None:
            return self.roles
        return self.roles | self.project_roles.get(project_id, frozenset())

    def has_role(self, role: Role, project_id: str | None = None) -> bool:
        return role in self.roles_for(project_id)


def create_access_token(
    principal: Principal, *, ttl_minutes: int | None = None
) -> tuple[str, datetime]:
    settings = get_settings()
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl)
    payload = {
        "sub": principal.user_id,
        "tid": principal.tenant_id,
        "email": principal.email,
        "roles": sorted(r.value for r in principal.roles),
        "project_roles": {
            pid: sorted(r.value for r in roles)
            for pid, roles in principal.project_roles.items()
        },
        "svc": principal.is_service_account,
        "mfa": principal.mfa,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, expires


def decode_access_token(token: str) -> Principal:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc

    def _roles(values) -> frozenset[Role]:
        out = set()
        for v in values or []:
            try:
                out.add(Role(v))
            except ValueError:
                continue  # role removed from the vocabulary since issuance
        return frozenset(out)

    return Principal(
        user_id=payload["sub"],
        tenant_id=payload["tid"],
        email=payload.get("email", ""),
        roles=_roles(payload.get("roles")),
        project_roles={
            pid: _roles(vals) for pid, vals in (payload.get("project_roles") or {}).items()
        },
        is_service_account=bool(payload.get("svc")),
        mfa=bool(payload.get("mfa")),
        token_id=payload.get("jti", ""),
    )


# --- API tokens (service accounts) ---------------------------------------

API_TOKEN_PREFIX = "fynix_pat_"


def generate_api_token() -> tuple[str, str]:
    """Return `(plaintext, hash)`. The plaintext is shown to the user once."""
    raw = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_api_token(raw)


def hash_api_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sign_webhook(secret: str, body: bytes, timestamp: str) -> str:
    """HMAC-SHA256 over `timestamp.body`, matching the documented scheme."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_webhook(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    return hmac.compare_digest(sign_webhook(secret, body, timestamp), signature)
