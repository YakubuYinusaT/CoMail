"""Settings, read from the environment once at import."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Absolute, so the app finds its own .env however it was launched.
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(default="", alias="DATABASE_URL")

    # Empty means JS-heavy pages are skipped and flagged rather than
    # rendered. Useful locally; in Cloud Run this points at the renderer.
    render_service_url: str = Field(default="", alias="RENDER_SERVICE_URL")
    render_timeout_ms: int = Field(default=20000, alias="RENDER_TIMEOUT_MS")

    # Provider is swappable; see app/llm.py. Keys for both may sit here,
    # only the selected one is required.
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    llm_model: str = Field(default="", alias="LLM_MODEL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    zoho_client_id: str = Field(default="", alias="ZOHO_CLIENT_ID")
    zoho_client_secret: str = Field(default="", alias="ZOHO_CLIENT_SECRET")
    zoho_refresh_token: str = Field(default="", alias="ZOHO_REFRESH_TOKEN")
    zoho_account_id: str = Field(default="", alias="ZOHO_ACCOUNT_ID")
    zoho_from_address: str = Field(default="", alias="ZOHO_FROM_ADDRESS")
    # Zoho is regional: an account created under .eu or .in will 401
    # against .com with an error that looks like a scope problem.
    zoho_api_base: str = Field(
        default="https://mail.zoho.com", alias="ZOHO_API_BASE"
    )

    internal_token: str = Field(default="", alias="INTERNAL_TOKEN")

    # Sending discipline. Defaults are the build spec's; raising them is a
    # deliberate act, not a convenience.
    send_daily_cap: int = Field(default=30, alias="SEND_DAILY_CAP")
    send_hourly_cap: int = Field(default=6, alias="SEND_HOURLY_CAP")
    send_window_start: str = Field(default="08:30", alias="SEND_WINDOW_START")
    send_window_end: str = Field(default="17:30", alias="SEND_WINDOW_END")
    send_timezone: str = Field(default="Africa/Lagos", alias="SEND_TIMEZONE")

    # Above this, two drafts in a batch read as variants of each other.
    # Tune on real batches; 0.72 is a starting guess, not a finding.
    sameness_threshold: float = Field(default=0.72, alias="SAMENESS_THRESHOLD")

    @property
    def sqlalchemy_url(self) -> str:
        """Point SQLAlchemy at psycopg 3.

        Neon hands out `postgresql://...`, which SQLAlchemy resolves to
        psycopg2 — a driver this image does not install. Rewriting here
        means the connection string pastes from the Neon console or
        Secret Manager unchanged.
        """
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://") :]
        return url


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
