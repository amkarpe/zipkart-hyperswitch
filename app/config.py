"""Settings loaded once from the environment (.env locally, Render env vars in production)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    hyperswitch_base_url: str
    hyperswitch_api_key: str          # secret: server only, never sent to the browser
    hyperswitch_publishable_key: str  # public: the only key the browser gets
    hyperswitch_profile_id: str
    hyperswitch_webhook_secret: str
    database_url: str
    app_env: str
    app_base_url: str
    store_console_password: str
    currency: str
    weight_buffer_percent: int
    substitution_timeout_seconds: int
    reconcile_after_minutes: int
    sweep_interval_seconds: int
    settlement_cutoff: str        # "HH:MM", Authorize.net Transaction Cut-Off Time
    settlement_timezone: str


def load_settings() -> Settings:
    return Settings(
        hyperswitch_base_url=_required("HYPERSWITCH_BASE_URL").rstrip("/"),
        hyperswitch_api_key=_required("HYPERSWITCH_API_KEY"),
        hyperswitch_publishable_key=_required("HYPERSWITCH_PUBLISHABLE_KEY"),
        hyperswitch_profile_id=_required("HYPERSWITCH_PROFILE_ID"),
        hyperswitch_webhook_secret=_required("HYPERSWITCH_WEBHOOK_SECRET"),
        database_url=_required("DATABASE_URL"),
        app_env=os.environ.get("APP_ENV", "development").strip(),
        app_base_url=_required("APP_BASE_URL").rstrip("/"),
        store_console_password=_required("STORE_CONSOLE_PASSWORD"),
        currency=os.environ.get("CURRENCY", "USD").strip().upper(),
        weight_buffer_percent=int(os.environ.get("WEIGHT_BUFFER_PERCENT", "15")),
        substitution_timeout_seconds=int(os.environ.get("SUBSTITUTION_TIMEOUT_SECONDS", "60")),
        reconcile_after_minutes=int(os.environ.get("RECONCILE_AFTER_MINUTES", "5")),
        sweep_interval_seconds=int(os.environ.get("SWEEP_INTERVAL_SECONDS", "180")),
        # Authorize.net's default cut-off is 3:00 PM Pacific (Account > Settings > Transaction Cut-Off Time).
        settlement_cutoff=os.environ.get("SETTLEMENT_CUTOFF", "15:00").strip(),
        settlement_timezone=os.environ.get("SETTLEMENT_TIMEZONE", "America/Los_Angeles").strip(),
    )


settings = load_settings()
