"""Agent registry — the only place agent types are resolved by name."""

from __future__ import annotations

from app.agents.base import Agent
from app.agents.build import (
    CodeAgent,
    DocumentationAgent,
    ReleaseAgent,
    ReviewAgent,
    SecurityAgent,
    TestAgent,
)
from app.agents.planning import (
    BriefAnalystAgent,
    EstimatorAgent,
    PlannerAgent,
    SolutionArchitectAgent,
)
from app.core.errors import ValidationError

_REGISTRY: dict[str, type[Agent]] = {
    BriefAnalystAgent.agent_type: BriefAnalystAgent,
    SolutionArchitectAgent.agent_type: SolutionArchitectAgent,
    EstimatorAgent.agent_type: EstimatorAgent,
    PlannerAgent.agent_type: PlannerAgent,
    CodeAgent.agent_type: CodeAgent,
    TestAgent.agent_type: TestAgent,
    ReviewAgent.agent_type: ReviewAgent,
    SecurityAgent.agent_type: SecurityAgent,
    ReleaseAgent.agent_type: ReleaseAgent,
    DocumentationAgent.agent_type: DocumentationAgent,
}


def get(agent_type: str) -> Agent:
    try:
        return _REGISTRY[agent_type]()
    except KeyError as exc:
        raise ValidationError(
            f"unknown agent type '{agent_type}'",
            details={"known": sorted(_REGISTRY)},
        ) from exc


def known_types() -> list[str]:
    return sorted(_REGISTRY)


def describe() -> list[dict]:
    return [
        {
            "agent_type": agent_type,
            "description": cls.description,
            "structured_output": cls.output_schema is not None,
            "optional": cls.optional,
        }
        for agent_type, cls in sorted(_REGISTRY.items())
    ]
