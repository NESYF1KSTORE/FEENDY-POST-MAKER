"""Runtime configuration.

Every value is environment-driven so the same image runs in dev, staging and
production (NFR-012). Secrets are never defaulted to a usable value: the app
refuses to boot in production with placeholder credentials.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- identity of the deployment ---
    env: str = Field(default="dev", alias="FYNIX_ENV")
    base_url: str = Field(default="http://localhost:8000", alias="FYNIX_BASE_URL")
    log_level: str = Field(default="INFO", alias="FYNIX_LOG_LEVEL")

    # --- storage ---
    database_url: str = Field(
        default="postgresql+psycopg://fynix:fynix@localhost:5432/fynix",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    artifact_dir: str = Field(default="/var/lib/fynix/artifacts", alias="ARTIFACT_DIR")

    # --- auth ---
    jwt_secret: str = Field(default=PLACEHOLDER_SECRET, alias="JWT_SECRET")
    jwt_ttl_minutes: int = Field(default=60, alias="JWT_TTL_MINUTES")
    bootstrap_admin_email: str = Field(
        default="admin@fynix.local", alias="BOOTSTRAP_ADMIN_EMAIL"
    )
    bootstrap_admin_password: str = Field(
        default="", alias="BOOTSTRAP_ADMIN_PASSWORD"
    )

    # --- AI gateway ---
    ai_default_provider: str = Field(default="mock", alias="AI_DEFAULT_PROVIDER")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_MODEL")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    ai_request_timeout_s: int = Field(default=120, alias="AI_REQUEST_TIMEOUT_S")

    # --- budgets (spec §14.1) ---
    default_project_budget_rub: float = Field(
        default=50_000.0, alias="DEFAULT_PROJECT_BUDGET_RUB"
    )
    budget_soft_pct: int = Field(default=50, alias="BUDGET_SOFT_PCT")
    budget_warning_pct: int = Field(default=80, alias="BUDGET_WARNING_PCT")

    # --- runners (spec §10.2) ---
    runner_driver: str = Field(default="local", alias="RUNNER_DRIVER")  # local|docker
    runner_image: str = Field(default="python:3.12-slim", alias="RUNNER_IMAGE")
    runner_timeout_s: int = Field(default=900, alias="RUNNER_TIMEOUT_S")
    runner_cpu: str = Field(default="1", alias="RUNNER_CPU")
    runner_memory: str = Field(default="1g", alias="RUNNER_MEMORY")
    runner_workspace_root: str = Field(
        default="/var/lib/fynix/workspaces", alias="RUNNER_WORKSPACE_ROOT"
    )

    # --- notifications ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    # Echoed back by Telegram in every webhook request; the only proof the
    # request really came from Telegram.
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")
    # "polling" works without a public HTTPS endpoint; "webhook" needs one.
    telegram_mode: str = Field(default="polling", alias="TELEGRAM_MODE")

    # --- worker ---
    worker_concurrency: int = Field(default=4, alias="WORKER_CONCURRENCY")
    worker_poll_interval_s: float = Field(default=1.0, alias="WORKER_POLL_INTERVAL_S")
    job_lease_seconds: int = Field(default=300, alias="JOB_LEASE_SECONDS")

    @field_validator("env")
    @classmethod
    def _known_env(cls, v: str) -> str:
        if v not in {"dev", "test", "staging", "prod"}:
            raise ValueError(f"unknown FYNIX_ENV={v}")
        return v

    @property
    def is_production(self) -> bool:
        return self.env in {"staging", "prod"}

    def assert_bootable(self) -> None:
        """Fail fast rather than run production with placeholder secrets."""
        if not self.is_production:
            return
        problems = []
        if self.jwt_secret in {PLACEHOLDER_SECRET, ""} or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be set to a random value >= 32 chars")
        if "fynix:fynix@" in self.database_url:
            problems.append("DATABASE_URL still uses the default dev password")
        if not self.base_url.startswith("https://"):
            problems.append("FYNIX_BASE_URL must be https in staging/prod")
        if problems:
            raise RuntimeError("refusing to start: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that mutate the environment."""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
