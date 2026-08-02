"""AI Gateway — the single door to every model (spec §4.2, §5.3, §5.4).

Responsibilities, in the order they are applied to a request:
  1. classify the payload and refuse egress that policy forbids;
  2. redact secrets from everything that leaves the process;
  3. check the project budget before spending anything;
  4. call the primary profile, retrying transient failures with an idempotency
     key so a timeout never double-bills;
  5. fall back only to a policy-compatible profile;
  6. validate structured output against the requested schema;
  7. write a cost ledger entry and return telemetry.

No workflow code talks to a provider directly.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.ai.providers.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Provider,
    estimate_tokens,
)
from app.ai.providers.mock import MockProvider
from app.ai.routing import ModelProfile, RoutingDecision, cost_rub, route
from app.config import get_settings
from app.core.classification import assert_egress_allowed, classify_text
from app.core.errors import PolicyConflict, ProviderError, ValidationError
from app.core.hashing import checksum
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.core.tenancy import cache_key
from app.models.base import DataClass
from app.services import budgets as budgets_service

log = get_logger("fynix.ai")

MAX_ATTEMPTS_PER_PROFILE = 2


def build_provider(name: str) -> Provider:
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        from app.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    if name == "deepseek":
        from app.ai.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider()
    raise PolicyConflict(
        f"unknown AI provider '{name}'", details={"reason": "unknown_provider"}
    )


@dataclass
class GatewayResult:
    text: str
    parsed: dict | list | None
    profile: ModelProfile
    prompt_tokens: int
    completion_tokens: int
    cost_rub: float
    latency_ms: int
    attempts: int
    used_fallback: bool
    cached: bool = False
    telemetry: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AIGateway:
    """Stateless facade; one instance per process is fine."""

    def __init__(self, provider_factory=build_provider) -> None:
        self._factory = provider_factory
        self._cache: dict[str, tuple[str, CompletionResponse]] = {}

    # -- public API --------------------------------------------------------

    def complete(
        self,
        session: Session,
        *,
        tenant_id: str,
        project_id: str,
        agent_type: str,
        messages: list[Message],
        data_class: DataClass = DataClass.INTERNAL,
        tenant_max_external: DataClass = DataClass.INTERNAL,
        json_schema: dict | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        stage: str = "",
        offline: bool | None = None,
        override_profile: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        idempotency_key: str = "",
        use_cache: bool = True,
        optional: bool = False,
    ) -> GatewayResult:
        settings = get_settings()
        if offline is None:
            offline = settings.ai_default_provider == "mock"

        prompt_text = "\n".join(m.content for m in messages)

        # 1. Classification — the declared class is a floor, not a promise.
        effective_class, signals = classify_text(prompt_text, data_class)
        if signals:
            log.warning(
                "ai.classification_escalated",
                tenant_id=tenant_id,
                project_id=project_id,
                signals=signals,
                declared=data_class.value,
                effective=effective_class.value,
            )

        decision: RoutingDecision = route(
            agent_type,
            data_class=effective_class,
            offline=offline,
            override_profile=override_profile,
        )

        # 2. Redaction before anything leaves the process.
        safe_messages = [Message(role=m.role, content=redact_text(m.content)) for m in messages]
        safe_prompt = "\n".join(m.content for m in safe_messages)

        # 3. Budget pre-flight.
        estimated = estimate_tokens(safe_prompt)
        estimated_cost = cost_rub(decision.primary, estimated, decision.primary.max_output_tokens)
        budgets_service.assert_can_spend(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            estimated_amount=estimated_cost,
            optional=optional,
        )

        request_key = cache_key(
            tenant_id,
            "prompt",
            checksum(
                {
                    "messages": [(m.role, m.content) for m in safe_messages],
                    "schema": json_schema,
                    "profile": decision.primary.key,
                }
            ),
        )
        if use_cache and request_key in self._cache:
            _, cached_response = self._cache[request_key]
            parsed = _parse_structured(cached_response.text, json_schema)
            return GatewayResult(
                text=cached_response.text,
                parsed=parsed,
                profile=decision.primary,
                prompt_tokens=0,
                completion_tokens=0,
                cost_rub=0.0,
                latency_ms=0,
                attempts=0,
                used_fallback=False,
                cached=True,
                telemetry={"cache": "hit"},
            )

        attempts = 0
        used_fallback = False
        last_error: Exception | None = None
        started = time.perf_counter()

        for profile in _profile_chain(decision):
            if profile is not decision.primary:
                used_fallback = True

            provider = self._factory(profile.provider)
            # 4. Egress policy is evaluated per profile — a fallback to an
            #    external provider must pass the same check as the primary.
            assert_egress_allowed(
                effective_class,
                tenant_max_external,
                getattr(provider, "is_external", True),
                provider=profile.provider,
            )

            request = CompletionRequest(
                messages=safe_messages,
                model=profile.model,
                max_output_tokens=max_output_tokens or profile.max_output_tokens,
                temperature=profile.temperature if temperature is None else temperature,
                json_schema=json_schema,
                idempotency_key=idempotency_key
                or checksum({"k": request_key, "profile": profile.key}),
            )

            for _ in range(MAX_ATTEMPTS_PER_PROFILE):
                attempts += 1
                try:
                    response = provider.complete(request)
                except ProviderError as exc:
                    last_error = exc
                    log.warning(
                        "ai.provider_error",
                        provider=profile.provider,
                        permanent=exc.permanent,
                        attempt=attempts,
                        error=str(exc)[:300],
                    )
                    if exc.permanent:
                        break
                    continue

                parsed = None
                if json_schema is not None:
                    try:
                        parsed = _parse_structured(response.text, json_schema)
                    except ValidationError as exc:
                        # Spec §15: invalid agent output triggers a correction run
                        # rather than being accepted as an artifact.
                        last_error = exc
                        log.warning(
                            "ai.schema_validation_failed",
                            provider=profile.provider,
                            attempt=attempts,
                            error=str(exc)[:300],
                        )
                        continue

                spent = cost_rub(profile, response.prompt_tokens, response.completion_tokens)
                budgets_service.record_cost(
                    session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    provider=profile.provider,
                    sku=profile.model,
                    quantity=response.total_tokens,
                    unit="token",
                    effective_rate=(
                        spent / response.total_tokens if response.total_tokens else 0.0
                    ),
                    amount=spent,
                    stage=stage,
                    task_id=task_id,
                    run_id=run_id,
                )

                if use_cache:
                    self._cache[request_key] = (tenant_id, response)

                total_latency = int((time.perf_counter() - started) * 1000)
                log.info(
                    "ai.completion",
                    tenant_id=tenant_id,
                    project_id=project_id,
                    run_id=run_id,
                    agent_type=agent_type,
                    provider=profile.provider,
                    model=profile.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost_rub=spent,
                    attempts=attempts,
                    used_fallback=used_fallback,
                )
                return GatewayResult(
                    text=response.text,
                    parsed=parsed,
                    profile=profile,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost_rub=spent,
                    latency_ms=total_latency,
                    attempts=attempts,
                    used_fallback=used_fallback,
                    telemetry={
                        "provider_latency_ms": response.latency_ms,
                        "finish_reason": response.finish_reason,
                        "routing_reason": decision.reason,
                        "data_class": effective_class.value,
                    },
                )

        raise ProviderError(
            f"all model profiles failed for agent '{agent_type}': {last_error}",
            details={
                "attempts": attempts,
                "primary": decision.primary.key,
                "fallback": decision.fallback.key if decision.fallback else None,
            },
        )

    def clear_cache(self) -> None:
        self._cache.clear()


def _profile_chain(decision: RoutingDecision) -> list[ModelProfile]:
    chain = [decision.primary]
    if decision.fallback is not None:
        chain.append(decision.fallback)
    return chain


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Models occasionally wrap JSON in prose or a fenced block."""
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped
    match = _JSON_BLOCK.search(text)
    if match:
        return match.group(1)
    first = min(
        (i for i in (stripped.find("{"), stripped.find("[")) if i != -1), default=-1
    )
    last = max(stripped.rfind("}"), stripped.rfind("]"))
    if first != -1 and last > first:
        return stripped[first : last + 1]
    return stripped


def _parse_structured(text: str, schema: dict | None) -> dict | list | None:
    if schema is None:
        return None
    try:
        parsed = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"model output is not valid JSON: {exc}") from exc
    validate_schema(parsed, schema)
    return parsed


def validate_schema(value, schema: dict, path: str = "$") -> None:
    """Minimal JSON Schema validation covering the subset the agents use.

    A dependency-free checker keeps the gateway's trusted surface small; it
    supports type, required, properties, items, enum, minimum/maximum and
    minItems, which is everything the agent contracts rely on.
    """
    expected = schema.get("type")
    if expected == "object" or "properties" in schema:
        if not isinstance(value, dict):
            raise ValidationError(f"{path}: expected object, got {type(value).__name__}")
        for key in schema.get("required", []):
            if key not in value:
                raise ValidationError(f"{path}: missing required property '{key}'")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                validate_schema(value[key], subschema, f"{path}.{key}")
        return

    if expected == "array":
        if not isinstance(value, list):
            raise ValidationError(f"{path}: expected array, got {type(value).__name__}")
        if len(value) < int(schema.get("minItems", 0)):
            raise ValidationError(
                f"{path}: expected at least {schema['minItems']} items, got {len(value)}"
            )
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{index}]")
        return

    if "enum" in schema:
        if value not in schema["enum"]:
            raise ValidationError(f"{path}: value {value!r} is not one of {schema['enum']}")
        return

    if expected == "string" and not isinstance(value, str):
        raise ValidationError(f"{path}: expected string, got {type(value).__name__}")
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValidationError(f"{path}: expected integer, got {type(value).__name__}")
    if expected == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise ValidationError(f"{path}: expected number, got {type(value).__name__}")
    if expected == "boolean" and not isinstance(value, bool):
        raise ValidationError(f"{path}: expected boolean, got {type(value).__name__}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path}: {value} is above maximum {schema['maximum']}")


#: Process-wide instance used by the agent runtime.
gateway = AIGateway()
