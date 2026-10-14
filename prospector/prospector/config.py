"""Runtime configuration (environment variables / .env). Secrets never live in Git."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_NOTICE_TEMPLATE = (
    "You are receiving this because {business_name} identified {organisation} ({website}) "
    "through public sources ({sources}) as a business that may benefit from WordPress "
    "support services. We hold business contact details only. To opt out or ask what we "
    "hold, email {contact_email}."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./data/prospector.db"
    secret_key: str = "change-me-in-production"
    suppression_salt: str = ""
    data_dir: Path = Path("./data")
    base_url: str = "http://localhost:8000"
    session_https_only: bool = False
    api_token: str | None = None
    log_level: str = "INFO"

    # Identity used in notices and exports (R7)
    business_name: str = "PB4HOST"
    notice_contact_email: str = "privacy@pb4host.com"
    notice_template: str = DEFAULT_NOTICE_TEMPLATE

    # Google
    google_maps_api_key: str | None = None
    psi_api_key: str | None = None
    places_region_code: str = "IN"
    places_language_code: str = "en"
    places_daily_page_cap: int = 200
    places_pages_per_run: int = 10
    psi_runs: int = 1
    psi_strategies: list[str] = ["mobile"]
    psi_daily_cap: int = 2000
    dns_daily_cap: int = 5000

    # Crawler conduct (R4)
    crawler_user_agent: str = (
        "PB4HOSTProspector/1.0 (+https://www.pb4host.com/prospector; prospector@pb4host.com)"
    )
    crawler_policy_url: str = "https://www.pb4host.com/prospector"
    crawler_min_interval_s: float = 1.0
    crawler_timeout_s: float = 10.0
    crawler_max_bytes: int = 2_000_000
    crawler_max_pages: int = 6
    crawler_active_probes: bool = False  # /readme.html and /wp-json/ fetches, default off
    crawler_allow_private_hosts: bool = False  # SSRF guard override for local development only
    crawler_daily_cap: int = 5000

    default_country: str = "IN"
    default_phone_region: str = "IN"
    error_recheck_delays_minutes: list[int] = [60, 1440]

    # Retention (R5) — days
    retention_raw_days: int = 30
    retention_coords_days: int = 30
    retention_perf_days: int = 365
    retention_unengaged_days: int = 365
    retention_person_days: int = 180
    retention_jobs_days: int = 30

    terms_review_max_age_days: int = 90  # R10

    latest_wordpress_version: str = "6.8"  # update periodically; used for "releases behind"
    php_min_supported: str = "8.2"  # versions below this count as end-of-life

    scoring_rules_path: Path | None = None
    worker_poll_interval_s: float = 2.0

    # --- Phase 2 -------------------------------------------------------------------------------
    # Inbound forms (R2 consent capture). The webhook secret signs payloads sent by pb4host.com.
    inbound_webhook_secret: str | None = None
    inbound_replay_window_s: int = 300
    inbound_consent_version: str = "2026-10-v1"
    privacy_notice_url: str = "https://www.pb4host.com/privacy"
    inbound_rate_limit_per_hour: int = 20  # hosted form submissions per client IP
    inbound_daily_cap: int = 2000

    # Sign In with LinkedIn (OpenID Connect) — only identity, never profile scraping
    linkedin_client_id: str | None = None
    linkedin_client_secret: str | None = None
    linkedin_daily_cap: int = 500

    # Owner's own LinkedIn connections export
    linkedin_connections_lia_reference: str = "LIA-2026-01 owner's own LinkedIn connections, 1:1 contact only"

    # Chrome UX Report API (field data per origin)
    crux_api_key: str | None = None  # falls back to psi_api_key
    crux_daily_cap: int = 1000

    # HTTP Archive + CrUX monthly discovery feed (BigQuery)
    bigquery_project: str | None = None
    bigquery_credentials_file: Path | None = None
    httparchive_max_bytes: int = 100 * 1024**3  # fail closed above this dry-run estimate
    httparchive_default_country: str = "in"
    httparchive_default_limit: int = 500
    httparchive_daily_cap: int = 2

    # GitHub organisation discovery (agency track)
    github_token: str | None = None
    github_daily_cap: int = 300
    github_pages_per_run: int = 3

    # Companies House (UK registry)
    companies_house_api_key: str | None = None
    companies_house_daily_cap: int = 500

    # CRM sync
    crm_target: str = "espocrm"
    espocrm_base_url: str | None = None
    espocrm_api_key: str | None = None
    crm_status_field: str = "type"  # Account field whose value is mapped back to a Prospector status
    crm_status_map: str = '{"Customer": "customer", "Partner": "customer", "Prospect": "contacted", "Dead": "disqualified"}'
    crm_push_persons: bool = True
    crm_daily_cap: int = 2000

    # Rights requests and scheduled re-checks
    rights_due_days: int = 30
    recheck_interval_days: int = 7
    recheck_daily_limit: int = 200
    recheck_psi_after_days: int = 28

    @property
    def linkedin_enabled(self) -> bool:
        return bool(self.linkedin_client_id and self.linkedin_client_secret)

    @property
    def crm_configured(self) -> bool:
        return bool(self.espocrm_base_url and self.espocrm_api_key)

    @property
    def salt(self) -> str:
        return self.suppression_salt or self.secret_key

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Testing helper: drop the cached settings so environment changes are picked up."""
    get_settings.cache_clear()
