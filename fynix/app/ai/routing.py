"""Model routing (spec §5.3).

Routing is decided by task complexity, data risk, latency and cost — never by
brand. Workflow code asks for a *capability*; this module answers with a model
profile. Every profile carries a fallback, and a fallback that would change the
processing region or data policy is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import PolicyConflict
from app.models.base import DataClass


@dataclass(frozen=True)
class ModelProfile:
    """Concrete provider/model selection plus the policy attributes it carries."""

    key: str
    provider: str
    model: str
    region: str
    max_output_tokens: int = 4096
    temperature: float = 0.2
    #: Cost per 1M tokens, RUB. Used by the cost ledger (§14.1).
    input_rate_per_mtok: float = 0.0
    output_rate_per_mtok: float = 0.0
    #: Highest classification this profile may process.
    max_data_class: DataClass = DataClass.CONFIDENTIAL
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "provider": self.provider,
            "model": self.model,
            "region": self.region,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "max_data_class": self.max_data_class.value,
        }


#: Rates are placeholders to be replaced by the ADR on provider selection
#: (decision D-04); they exist so unit economics are measured from day one.
PROFILES: dict[str, ModelProfile] = {
    "reasoning-high": ModelProfile(
        key="reasoning-high",
        provider="anthropic",
        model="claude-sonnet-5",
        region="us",
        max_output_tokens=8192,
        temperature=0.2,
        input_rate_per_mtok=280.0,
        output_rate_per_mtok=1400.0,
        max_data_class=DataClass.CONFIDENTIAL,
        tags=("architecture", "review", "security"),
    ),
    "reasoning-cheap": ModelProfile(
        key="reasoning-cheap",
        provider="deepseek",
        model="deepseek-chat",
        region="cn",
        max_output_tokens=8192,
        temperature=0.2,
        input_rate_per_mtok=25.0,
        output_rate_per_mtok=100.0,
        max_data_class=DataClass.INTERNAL,
        tags=("bulk", "docs", "tests"),
    ),
    "local-mock": ModelProfile(
        key="local-mock",
        provider="mock",
        model="mock-1",
        region="local",
        max_output_tokens=4096,
        temperature=0.0,
        input_rate_per_mtok=0.0,
        output_rate_per_mtok=0.0,
        max_data_class=DataClass.RESTRICTED,
        tags=("offline", "ci"),
    ),
}


@dataclass(frozen=True)
class RoutingDecision:
    primary: ModelProfile
    fallback: ModelProfile | None
    reason: str


#: agent_type -> (primary key, fallback key). High-risk reasoning never falls
#: back to the cheap tier; bulk generation does.
AGENT_ROUTES: dict[str, tuple[str, str | None]] = {
    "brief_analyst": ("reasoning-cheap", "reasoning-high"),
    "solution_architect": ("reasoning-high", None),
    "estimator": ("reasoning-cheap", "reasoning-high"),
    "planner": ("reasoning-high", "reasoning-cheap"),
    "code": ("reasoning-high", None),
    "test": ("reasoning-cheap", "reasoning-high"),
    "review": ("reasoning-high", None),
    "security": ("reasoning-high", None),
    "release": ("reasoning-cheap", "reasoning-high"),
    "documentation": ("reasoning-cheap", "reasoning-high"),
}


def get_profile(key: str) -> ModelProfile:
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise PolicyConflict(
            f"unknown model profile '{key}'", details={"reason": "unknown_model_profile"}
        ) from exc


def fallback_allowed(primary: ModelProfile, fallback: ModelProfile) -> tuple[bool, str]:
    """Spec §5.3: a fallback that changes region or data policy is forbidden."""
    if fallback.region != primary.region:
        return False, "fallback_changes_region"
    if fallback.max_data_class.rank < primary.max_data_class.rank:
        return False, "fallback_lowers_data_policy"
    return True, "allowed"


def route(
    agent_type: str,
    *,
    data_class: DataClass = DataClass.INTERNAL,
    offline: bool = False,
    override_profile: str | None = None,
) -> RoutingDecision:
    """Pick primary and (optional) fallback profiles for an agent run."""
    if offline:
        return RoutingDecision(PROFILES["local-mock"], None, "offline_mode")

    if override_profile:
        profile = get_profile(override_profile)
        if data_class.rank > profile.max_data_class.rank:
            raise PolicyConflict(
                "requested model profile may not process this data class",
                details={
                    "reason": "data_class_exceeds_profile",
                    "profile": profile.key,
                    "data_class": data_class.value,
                },
            )
        return RoutingDecision(profile, None, "explicit_override")

    primary_key, fallback_key = AGENT_ROUTES.get(agent_type, ("reasoning-cheap", "reasoning-high"))
    primary = get_profile(primary_key)

    # Data class outranks the cost preference: downgrade to a profile that is
    # allowed to see this data, or refuse.
    if data_class.rank > primary.max_data_class.rank:
        candidates = [
            p
            for p in PROFILES.values()
            if p.max_data_class.rank >= data_class.rank and p.key != primary.key
        ]
        if not candidates:
            raise PolicyConflict(
                "no model profile is approved for this data class",
                details={"reason": "no_profile_for_data_class", "data_class": data_class.value},
            )
        primary = min(candidates, key=lambda p: p.output_rate_per_mtok)
        return RoutingDecision(primary, None, "data_class_constrained")

    fallback = None
    if fallback_key:
        candidate = get_profile(fallback_key)
        allowed, reason = fallback_allowed(primary, candidate)
        if allowed and data_class.rank <= candidate.max_data_class.rank:
            fallback = candidate
        else:
            _ = reason  # recorded by the gateway when a fallback is attempted

    return RoutingDecision(primary, fallback, "agent_route")


def cost_rub(profile: ModelProfile, prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1_000_000 * profile.input_rate_per_mtok
        + completion_tokens / 1_000_000 * profile.output_rate_per_mtok,
        6,
    )
