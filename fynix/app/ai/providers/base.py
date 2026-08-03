"""Provider port. Swapping vendors must not touch workflow code (spec §4.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: list[Message]
    model: str
    max_output_tokens: int = 4096
    temperature: float = 0.2
    # When set, the provider is asked for JSON matching this schema and the
    # gateway validates the result before it becomes an artifact (§5.3).
    json_schema: dict | None = None
    stop: list[str] = field(default_factory=list)
    idempotency_key: str = ""


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    finish_reason: str = "stop"
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Provider(Protocol):
    name: str
    #: True when the payload leaves our perimeter — drives the egress check.
    is_external: bool

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        ...


def estimate_tokens(text: str) -> int:
    """Cheap pre-flight estimate used for budget forecasting before a call.

    ~3.5 characters per token is a reasonable blend for mixed Russian/English
    technical text; it only needs to be good enough to catch a request that
    would obviously blow the remaining budget.
    """
    return max(1, int(len(text) / 3.5))
