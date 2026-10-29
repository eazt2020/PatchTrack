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

from .secret_files import secret_file_settings


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
    secrets_key: str = Field(
        "",
        description=(
            "Optional key for encrypting credentials saved through the UI "
            "(currently the SMTP password). Falls back to jwt_secret. Changing "
            "either key makes stored credentials unreadable and they must be "
            "entered again."
        ),
    )
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
    # The installer writes the release tag into .env. It is recorded in every
    # data archive so an operator can confirm that the target installation is
    # the same release before restoring.
    version: str = Field("", description="PatchTrack release version")
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

    # Data archives are written by the backup worker container onto a volume
    # that the reporting service mounts read-only. The retention count is an
    # initial default; the live value is in shared.app_settings and is edited
    # in the console. The worker applies it after each successful archive.
    backup_dir: Path = Path("/var/lib/patchtrack/backups")
    backup_interval: int = Field(86400, ge=60)
    backup_retention: int = Field(7, ge=1, le=90)

    # TLS termination inside the gateway. When enabled, the gateway serves
    # HTTPS on its normal port instead of HTTP, so a first installation is
    # encrypted without a separate proxy. A self-signed certificate is
    # generated on first start; an administrator can upload the organisation's
    # own certificate from Settings afterwards. Set enabled=false when an
    # external reverse proxy terminates TLS.
    tls_enabled: bool = False
    tls_dir: Path = Path("/var/lib/patchtrack/tls")
    tls_cert_file: str = Field(
        "",
        description="Certificate path; relative names resolve under tls_dir",
    )
    tls_key_file: str = Field(
        "",
        description="Private-key path; relative names resolve under tls_dir",
    )
    tls_hostname: str = Field(
        "",
        description=(
            "DNS name placed in the generated self-signed certificate; the "
            "host's name is used when empty"
        ),
    )

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # A *_FILE value is a deliberate choice to take the credential out of
        # the environment, so it outranks the plain variable. Explicit
        # constructor arguments (used by tests and tooling) still win.
        return (
            init_settings,
            secret_file_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

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


def release_version() -> str:
    """The release the running image was built from.

    Used for API metadata and the console so neither can drift from the
    installed release. The installer writes PATCHTRACK_VERSION into .env;
    outside a deployment the value is absent and reported as ``unknown``.
    """
    return get_settings().version or "unknown"
