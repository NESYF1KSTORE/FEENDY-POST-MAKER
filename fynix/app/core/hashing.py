"""Deterministic checksums used for artifacts, approvals and the audit chain."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no insignificant whitespace, UTF-8 preserved.

    Two structurally equal payloads must produce the same string, otherwise
    checksum comparison (approval staleness, artifact identity) is unreliable.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def checksum(value: Any) -> str:
    """`sha256:<hex>` over the canonical JSON form of `value`."""
    return "sha256:" + sha256_hex(canonical_json(value))


def digest_file(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return "sha256:" + h.hexdigest()
