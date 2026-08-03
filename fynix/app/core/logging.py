"""Structured logging with correlation ids (NFR-010).

Every log line carries tenant/project/run/correlation ids so a single request
can be reconstructed across the API, the worker and the AI gateway.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

import structlog

from app.core.redaction import redact

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
# The default must not be a shared mutable dict: every context that never
# called bind_context() would otherwise alias the same object.
_context: ContextVar[dict | None] = ContextVar("log_context", default=None)


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


def bind_context(**kwargs: str) -> None:
    current = dict(_context.get() or {})
    current.update({k: v for k, v in kwargs.items() if v})
    _context.set(current)


def clear_context() -> None:
    _context.set(None)
    _correlation_id.set("")


def _inject_context(_logger, _method, event_dict: dict) -> dict:
    cid = _correlation_id.get()
    if cid:
        event_dict.setdefault("correlation_id", cid)
    for key, value in (_context.get() or {}).items():
        event_dict.setdefault(key, value)
    return event_dict


def _redact_processor(_logger, _method, event_dict: dict) -> dict:
    return redact(event_dict)


def configure(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        _redact_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "fynix"):
    return structlog.get_logger(name)
