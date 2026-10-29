"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Runtime settings. Secrets must come from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Employee Discovery Pipeline"
    app_env: str = "development"
    debug: bool = False
    api_key: str | None = Field(
        default=None,
        description="Optional API key for protecting public endpoints.",
    )

    apollo_api_key: str = ""
    email_verifier_api_key: str = ""
    email_verifier_provider: str = "zerobounce"
    email_verifier_enabled: bool = False

    database_url: str = "sqlite:///./employee_discovery.db"
    redis_url: str = "redis://localhost:6379/0"

    apollo_required_email_status: str = "verified"
    external_required_email_status: str = "valid"

    batch_size: int = 50
    max_retries: int = 3
    request_timeout: float = 30.0
    apollo_concurrency: int = 5
    email_verifier_concurrency: int = 5

    target_job_titles: list[str] = Field(
        default_factory=lambda: [
            "Sales",
            "Marketing",
            "VP Sales",
            "Head of Sales",
            "VP Marketing",
        ]
    )

    max_upload_size_mb: int = 10
    cors_origins: list[str] = Field(
        default_factory=list,
        description="Optional CORS allow-list. Empty disables CORS middleware.",
    )
    quick_run_max_companies: int = Field(
        default=500,
        description="Max companies allowed for in-process UI quick-run (no Celery).",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings.

    Call ``get_settings.cache_clear()`` after changing ``.env`` (uvicorn
    ``--reload`` does not watch ``.env`` files).
    """
    return Settings()


def reload_settings() -> Settings:
    """Drop the settings cache and reload from ``.env`` / environment."""
    get_settings.cache_clear()
    return get_settings()
