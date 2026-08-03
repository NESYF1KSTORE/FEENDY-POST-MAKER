"""Agent runtime: the execution contract shared by every agent (spec §5).

An agent is a declarative object — system prompt, output schema, tool needs and
a `build_messages` method. `execute()` wraps it in the Agent Run contract from
§5.2: it opens a run record, assembles context with trust levels, screens for
prompt injection, calls the gateway, validates the output, stores evidence and
closes the run with cost and provenance attached.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from sqlalchemy.orm import Session

from app.ai.gateway import GatewayResult
from app.ai.gateway import gateway as default_gateway
from app.ai.providers.base import Message
from app.core import audit
from app.core.classification import UNTRUSTED, detect_injection, trust_level, wrap_untrusted
from app.core.errors import PolicyConflict
from app.core.hashing import checksum
from app.core.logging import get_logger
from app.core.policy import resolve_tool_grants
from app.models.base import DataClass, RunStatus, utcnow
from app.models.execution import AgentRun
from app.orchestrator import events

log = get_logger("fynix.agents")

PROMPT_TEMPLATE_VERSION = "v1"


@dataclass
class ContextItem:
    """One piece of context with its provenance, so trust can be reasoned about."""

    source: str
    label: str
    content: str

    @property
    def trust(self) -> str:
        return trust_level(self.source)


@dataclass
class AgentContext:
    tenant_id: str
    project_id: str
    task_id: str | None = None
    parent_run_id: str | None = None
    data_class: DataClass = DataClass.INTERNAL
    tenant_max_external: DataClass = DataClass.INTERNAL
    correlation_id: str = ""
    offline: bool | None = None
    items: list[ContextItem] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    def add(self, source: str, label: str, content: str) -> AgentContext:
        self.items.append(ContextItem(source=source, label=label, content=content))
        return self


@dataclass
class AgentResult:
    run: AgentRun
    output: dict | list | None
    text: str
    cost_rub: float
    tokens: int

    @property
    def succeeded(self) -> bool:
        return self.run.status == RunStatus.SUCCEEDED.value


class Agent(ABC):
    """Base class. Subclasses declare their contract; they never call providers."""

    #: Stable identifier used for routing, tool grants and audit.
    agent_type: str = ""
    #: JSON Schema the output must satisfy. None means free-form text.
    output_schema: ClassVar[dict | None] = None
    #: Human-readable purpose recorded on the run.
    description: str = ""
    #: When True the run is discretionary and is dropped at the 80% budget mark.
    optional: bool = False

    @abstractmethod
    def system_prompt(self) -> str:
        """Trusted instructions. Never built from user-supplied text."""

    def user_prompt(self, context: AgentContext) -> str:
        """Task statement. Untrusted items are fenced by `build_messages`."""
        return "Выполни задачу согласно системной инструкции."

    def build_messages(self, context: AgentContext) -> list[Message]:
        """Assemble the final message list with trust boundaries made explicit."""
        parts: list[str] = [self.user_prompt(context)]
        for item in context.items:
            if item.trust == UNTRUSTED:
                parts.append(f"### {item.label}\n{wrap_untrusted(item.source, item.content)}")
            else:
                parts.append(f"### {item.label}\n{item.content}")
        return [
            Message(role="system", content=self.system_prompt()),
            Message(role="user", content="\n\n".join(parts)),
        ]

    def prompt_checksum(self) -> str:
        return checksum({"system": self.system_prompt(), "schema": self.output_schema})


def screen_context(context: AgentContext) -> list[dict]:
    """Report injection attempts found in untrusted context (spec §5.4)."""
    findings: list[dict] = []
    for item in context.items:
        if item.trust != UNTRUSTED:
            continue
        for pattern in detect_injection(item.content):
            findings.append({"source": item.source, "label": item.label, "pattern": pattern})
    return findings


def execute(
    session: Session,
    agent: Agent,
    context: AgentContext,
    *,
    gateway=None,
    stage: str = "",
    override_profile: str | None = None,
    requested_tools: list[str] | None = None,
) -> AgentResult:
    """Run one agent end to end under the §5.2 contract."""
    gw = gateway or default_gateway
    tool_grants = resolve_tool_grants(agent.agent_type, requested_tools)

    run = AgentRun(
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        task_id=context.task_id,
        parent_run_id=context.parent_run_id,
        agent_type=agent.agent_type,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_checksum=agent.prompt_checksum(),
        tool_grants=tool_grants,
        data_class=context.data_class.value,
        correlation_id=context.correlation_id,
        status=RunStatus.RUNNING.value,
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()

    injection_findings = screen_context(context)
    if injection_findings:
        # Spec §5.4: on a conflict between instructions, stop and escalate.
        run.status = RunStatus.FAILED.value
        run.reason = "policy_conflict"
        run.finished_at = utcnow()
        run.evidence = {"injection_findings": injection_findings}
        session.flush()
        audit.record(
            session,
            tenant_id=context.tenant_id,
            action="agent.run_blocked",
            actor_type="system",
            resource_type="agent_run",
            resource_id=run.id,
            decision="deny",
            correlation_id=context.correlation_id,
            payload={"reason": "prompt_injection_detected", "findings": injection_findings},
        )
        raise PolicyConflict(
            "untrusted context contains instruction-injection markers; escalated to a human",
            details={"reason": "policy_conflict", "findings": injection_findings},
        )

    messages = agent.build_messages(context)

    try:
        result: GatewayResult = gw.complete(
            session,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            agent_type=agent.agent_type,
            messages=messages,
            data_class=context.data_class,
            tenant_max_external=context.tenant_max_external,
            json_schema=agent.output_schema,
            run_id=run.id,
            task_id=context.task_id,
            stage=stage,
            offline=context.offline,
            override_profile=override_profile,
            optional=agent.optional,
        )
    except Exception as exc:
        run.status = RunStatus.FAILED.value
        run.reason = type(exc).__name__
        run.finished_at = utcnow()
        run.evidence = {"error": str(exc)[:2000]}
        session.flush()
        events.emit(
            session,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            event_type=events.AGENT_RUN_COMPLETED,
            payload={"run_id": run.id, "agent_type": agent.agent_type, "status": "failed"},
        )
        raise

    run.status = RunStatus.SUCCEEDED.value
    run.reason = "ok"
    run.model_profile = result.profile.as_dict()
    run.prompt_tokens = result.prompt_tokens
    run.completion_tokens = result.completion_tokens
    run.cost_rub = result.cost_rub
    run.latency_ms = result.latency_ms
    run.output = result.parsed if isinstance(result.parsed, dict) else {"value": result.parsed}
    run.evidence = {
        "attempts": result.attempts,
        "used_fallback": result.used_fallback,
        "cached": result.cached,
        "context_sources": [
            {"source": i.source, "label": i.label, "trust": i.trust} for i in context.items
        ],
        **result.telemetry,
    }
    run.finished_at = utcnow()
    session.flush()

    audit.record(
        session,
        tenant_id=context.tenant_id,
        action="agent.run_completed",
        actor_type="agent",
        actor_id=agent.agent_type,
        resource_type="agent_run",
        resource_id=run.id,
        correlation_id=context.correlation_id,
        payload={
            "model": result.profile.model,
            "provider": result.profile.provider,
            "tokens": result.total_tokens,
            "cost_rub": result.cost_rub,
            "tool_grants": tool_grants,
        },
    )
    events.emit(
        session,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        event_type=events.AGENT_RUN_COMPLETED,
        payload={
            "run_id": run.id,
            "agent_type": agent.agent_type,
            "status": "succeeded",
            "cost_rub": result.cost_rub,
            "tokens": result.total_tokens,
        },
    )

    return AgentResult(
        run=run,
        output=result.parsed,
        text=result.text,
        cost_rub=result.cost_rub,
        tokens=result.total_tokens,
    )
