"""Secret redaction applied before anything is logged, audited or sent to a model.

Spec NFR-008: no secret values in prompts, logs, artifacts or source control.
This is defence in depth — the primary control is that agents never receive
secret values in the first place (`app.core.policy.FORBIDDEN_TOOLS`).
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "«redacted»"

#: Keys whose value is replaced wholesale, regardless of content.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "private_key",
        "authorization",
        "cookie",
        "session",
        "credential",
        "credentials",
        "password_hash",
        "jwt_secret",
        "webhook_secret",
        "ssh_key",
        "connection_string",
        "dsn",
    }
)

#: High-signal value patterns. Ordered most specific first.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("url_credentials", re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+):[^\s/@]+@")),
    ("generic_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
    )),
)


def redact_text(value: str) -> str:
    if not value:
        return value
    out = value
    for name, pattern in PATTERNS:
        if name == "url_credentials":
            out = pattern.sub(r"\1://" + REDACTED + "@", out)
        elif name == "generic_assignment":
            out = pattern.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or any(s in lowered for s in ("password", "secret", "token", "api_key"))


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-shaped structure. Depth-limited to stay cheap."""
    if _depth > 12:
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact(item, _depth=_depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def find_secrets(text: str) -> list[dict]:
    """Detector used by the G1 secret-scan gate (FR-013, AC-08)."""
    findings: list[dict] = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            snippet = match.group(0)
            findings.append(
                {
                    "rule": name,
                    "start": match.start(),
                    "preview": snippet[:6] + "…" if len(snippet) > 6 else "…",
                }
            )
    return findings
