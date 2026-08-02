"""Deterministic in-process provider.

It exists so the entire pipeline — intake, blueprint, planning, code, gates,
release — runs end to end in CI and on a laptop with no API keys and no spend.
Responses are derived from the requested JSON schema, so downstream schema
validation is exercised for real rather than bypassed.
"""

from __future__ import annotations

import hashlib
import json
import time

from app.ai.providers.base import CompletionRequest, CompletionResponse, estimate_tokens


class MockProvider:
    name = "mock"
    is_external = False

    def __init__(self, latency_ms: int = 5) -> None:
        self._latency_ms = latency_ms

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        prompt = "\n".join(m.content for m in request.messages)

        if request.json_schema is not None:
            text = json.dumps(
                _synthesize(request.json_schema, prompt), ensure_ascii=False, indent=2
            )
        else:
            text = (
                "[mock] Ответ сгенерирован локальным провайдером без обращения к внешней модели. "
                f"Длина контекста: {len(prompt)} символов."
            )

        elapsed = int((time.perf_counter() - started) * 1000) or self._latency_ms
        return CompletionResponse(
            text=text,
            model=request.model,
            provider=self.name,
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=estimate_tokens(text),
            latency_ms=elapsed,
            raw={"mock": True},
        )


#: Fields that reference *other* items by key. Synthesising a value for these
#: naively would make an item depend on itself, so they get graph-aware values.
REFERENCE_FIELDS = frozenset({"depends_on", "change_set_ids", "adr_refs"})

#: A list of objects carrying both `key` and `depends_on` is a DAG; generating
#: a small chain makes offline runs produce a plan worth executing.
DAG_ITEM_COUNT = 3

#: Lists whose natural empty state is "nothing found". Fabricating an entry here
#: would make every offline run look like a failed security scan.
EMPTY_BY_DEFAULT = frozenset(
    {
        "findings",
        "open_questions",
        "risks",
        "uncovered_risks",
        "follow_ups",
        "dependencies_added",
        "contract_failures",
        "waivers",
    }
)

#: Severity-shaped enums: pick the mildest value rather than the first one.
SEVERITY_FIELDS = frozenset(
    {"severity", "impact", "probability", "risk_level", "confidence", "regression_risk"}
)
MILD_VALUES = ("info", "low", "none")

#: Score-shaped integers. The schema minimum is 0, but a 0 here means "this
#: brief is unusable", which would stall every offline run at the first gate.
SCORE_FIELDS = frozenset({"completeness", "score", "coverage"})


def _synthesize(schema: dict, prompt: str, depth: int = 0, field: str | None = None, index: int = 0):
    """Build the smallest *coherent* value that satisfies `schema`.

    "Coherent" matters: the offline provider drives the same planner and code
    paths as a real model, so its output has to be internally consistent — task
    keys unique, dependencies pointing backwards, no self-references.
    """
    if depth > 6:
        return None
    schema_type = schema.get("type")

    if "enum" in schema:
        if field in SEVERITY_FIELDS:
            for candidate in MILD_VALUES:
                if candidate in schema["enum"]:
                    return candidate
        return schema["enum"][0]

    if schema_type == "object" or "properties" in schema:
        props: dict = schema.get("properties", {})
        required = schema.get("required", list(props))
        out = {}
        for key in props:
            if key not in required and depth != 0:
                continue
            if key == "key":
                out[key] = f"T{index + 1}"
            elif key in REFERENCE_FIELDS:
                out[key] = [f"T{index}"] if index > 0 else []
            else:
                out[key] = _synthesize(props[key], prompt, depth + 1, field=key, index=index)
        return out

    if schema_type == "array":
        if field in REFERENCE_FIELDS:
            return [f"T{index}"] if index > 0 else []
        if field in EMPTY_BY_DEFAULT and "minItems" not in schema:
            return []
        items = schema.get("items", {"type": "string"})
        item_props = items.get("properties", {}) if isinstance(items, dict) else {}
        is_dag = "key" in item_props and "depends_on" in item_props
        count = DAG_ITEM_COUNT if is_dag else max(1, int(schema.get("minItems", 1)))
        return [
            _synthesize(items, prompt, depth + 1, field=field, index=i) for i in range(count)
        ]

    if schema_type == "integer":
        if field in SCORE_FIELDS and "maximum" in schema:
            return int(schema["maximum"] * 0.9)
        return int(schema.get("minimum", 1))
    if schema_type == "number":
        return float(schema.get("minimum", 1))
    if schema_type == "boolean":
        return True

    # Distinct tasks must yield distinct files, otherwise the second change set
    # reproduces the first one and git reports nothing to commit.
    salt = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    if field == "path":
        return f"src/mock_{salt}_{index + 1}.py"
    if field == "branch":
        return f"feature/mock-{salt}"
    if field == "content":
        return (
            '"""Generated offline by the mock provider."""\n\n'
            f'MODULE_ID = "{salt}-{index + 1}"\n'
        )
    if field == "version":
        return "0.1.0"

    description = schema.get("description", "")
    return f"[mock] {description}".strip() or "[mock]"
