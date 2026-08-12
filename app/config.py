"""Application settings.

Every knob the reliability machinery depends on is declared here so the
operating envelope of the agent is inspectable in one place, and so CI, Docker
Compose and local runs configure it the same way.
"""

from datetime import time, timedelta, timezone
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The office runs on a fixed UTC-3 offset. Uruguay does not observe DST, so a
# fixed offset is exact and avoids a tzdata dependency in the container.
OFFICE_TZ = timezone(timedelta(hours=-3))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUDDLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Booking domain -----------------------------------------------------
    slot_minutes: int = 30
    max_booking_hours: int = 3
    max_booking_horizon_days: int = 90
    min_attendees: int = 1
    business_start: time = time(8, 0)
    business_end: time = time(20, 0)

    # --- Persistence --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://huddle:huddle@localhost:5433/huddle"
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- Auth ---------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-change-me-now")
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24

    # --- Seeding ------------------------------------------------------------
    seed_on_startup: bool = False
    seed_user_password: SecretStr = SecretStr("huddle-dev-password")

    # --- LLM ----------------------------------------------------------------
    llm_provider: Literal["openai", "anthropic", "fake"] = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: SecretStr = SecretStr("")
    llm_temperature: float = 0.0

    # --- Holds --------------------------------------------------------------
    # A hold is the reservation of a room that has not been confirmed yet. It
    # blocks everyone else, so its lifetime is deliberately short.
    hold_ttl_seconds: int = 120
    hold_sweeper_interval_seconds: int = 10
    hold_sweeper_enabled: bool = True

    # --- Tool reliability ---------------------------------------------------
    tool_timeout_seconds: float = 10.0
    tool_max_attempts: int = 3
    tool_backoff_base_seconds: float = 0.2
    tool_backoff_multiplier: float = 2.0
    tool_backoff_max_seconds: float = 5.0
    tool_backoff_jitter: float = 0.1

    # --- Guardrails ---------------------------------------------------------
    max_tool_calls_per_conversation: int = 25
    max_tokens_per_conversation: int = 60_000
    max_llm_calls_per_turn: int = 8
    require_confirmation_for_high_risk: bool = True
    confirmation_ttl_seconds: int = 600

    # --- Hold-cycling detection --------------------------------------------
    abuse_window_seconds: int = 1800
    abuse_min_holds: int = 4
    abuse_max_confirm_ratio: float = 0.34
    abuse_max_holds_per_window: int = 10
    abuse_rate_limit_seconds: int = 900

    # --- Observability ------------------------------------------------------
    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "huddle-agent"
    otel_console_export: bool = False

    # --- Failure injection --------------------------------------------------
    failure_injection_enabled: bool = False

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            # A bare postgresql:// URL would silently pick the sync driver and
            # deadlock the event loop on first query. Fail loudly instead.
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def slot_delta(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)

    @property
    def sync_database_url(self) -> str:
        """The same database, addressed with a driver Alembic can use."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
