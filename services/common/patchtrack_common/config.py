"""Shared configuration for all PatchTrack services.

All settings are read from environment variables prefixed with ``PATCHTRACK_``.
No credential has a built-in production default: the process refuses to start
without a database URL and a JWT signing secret.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PATCHTRACK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@host:5432/patchtrack",
    )
    database_pool_size: int = 5
    database_pool_max_overflow: int = 5

    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_hours: int = 8
    service_token: str = Field(
        "",
        description="Shared bearer token used by the scheduler for internal calls",
    )

    environment: str = "development"
    session_cookie_secure: str = Field(
        "auto",
        pattern="^(auto|always|never)$",
        description=(
            "Secure flag on the session and CSRF cookies. 'auto' (the only "
            "sane default) marks them Secure for every non-loopback host, so "
            "a plain-HTTP development session cannot be used. 'never' exists "
            "only to reach a development instance by IP over HTTP and is "
            "refused when environment=production."
        ),
    )
    app_name: str = "PatchTrack"
    log_level: str = "INFO"
    max_request_bytes: int = Field(
        64 * 1024 * 1024,
        ge=1024,
        description=(
            "Largest accepted request body. Evidence attachments arrive "
            "base64-encoded inside a JSON request, so this must comfortably "
            "exceed the combined attachment size."
        ),
    )

    report_dir: Path = Path("/var/lib/patchtrack/reports")
    report_retention_days: int = 2555
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_sender: str = "patchtrack@localhost"

    assetexplorer_base_url: str = ""
    satellite_base_url: str = ""
    sccm_base_url: str = ""
    solaris_inbox: Path = Path("/var/lib/patchtrack/solaris-inbox")

    grafana_url: str = "http://127.0.0.1:3000"

    # In-app scheduler. Each service runs a background loop for the jobs it
    # owns, so no external timer or cron entry is required. Intervals are in
    # seconds; a failed run is retried after scheduler_retry_seconds rather
    # than waiting a full interval.
    scheduler_enabled: bool = True
    scheduler_poll_seconds: int = Field(15, ge=1, le=3600)
    scheduler_retry_seconds: int = Field(300, ge=1)
    scheduler_startup_delay_seconds: int = Field(10, ge=0, le=600)
    sync_assetexplorer_interval: int = Field(21600, ge=60)
    sync_satellite_interval: int = Field(21600, ge=60)
    sync_sccm_interval: int = Field(86400, ge=60)
    sync_solaris_interval: int = Field(604800, ge=60)
    daily_snapshot_interval: int = Field(86400, ge=300)
    audit_purge_interval: int = Field(86400, ge=300)
    report_tick_interval: int = Field(60, ge=10)
    report_purge_interval: int = Field(86400, ge=300)

    identity_url: str = "http://127.0.0.1:8001"
    inventory_url: str = "http://127.0.0.1:8002"
    compliance_url: str = "http://127.0.0.1:8003"
    reporting_url: str = "http://127.0.0.1:8004"

    @model_validator(mode="after")
    def _refuse_insecure_cookies_in_production(self) -> "Settings":
        # 'never' is the escape hatch for a plain-HTTP development instance
        # reached by IP from another host. In production it would leave the
        # session cookie readable on the wire, so the process refuses to start
        # rather than quietly weakening every deployment that copied a sample
        # environment file.
        if (self.environment.strip().lower() == "production"
                and self.session_cookie_secure == "never"):
            raise ValueError(
                "PATCHTRACK_SESSION_COOKIE_SECURE=never is a development-only "
                "setting and cannot be combined with "
                "PATCHTRACK_ENVIRONMENT=production. Terminate TLS in front of "
                "the gateway instead."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
