"""When a capture is safe to refund.

Authorize.net only credits settled transactions. A capture joins the batch of the first
Transaction Cut-Off after it; that batch settles some time later. So the earliest
sensible refund attempt is:

    gate(t) = first SETTLEMENT_CUTOFF (in SETTLEMENT_TIMEZONE) strictly after t, + margin

The cut-off is a wall-clock time in a named zone (zoneinfo), so it stays 4:00 PM local
across the PDT/PST change (23:00Z in summer, 00:00Z in winter). The margin is real
elapsed time, so it's added in UTC (Python adds timedeltas to zoned datetimes in wall
time).
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def first_cutoff_after(t: datetime, cutoff: str | None = None, tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or settings.settlement_timezone)
    hour, minute = (int(x) for x in (cutoff or settings.settlement_cutoff).split(":"))
    local = t.astimezone(tz)
    candidate = datetime(local.year, local.month, local.day, hour, minute, tzinfo=tz)
    if candidate <= local:
        nxt = local.date() + timedelta(days=1)   # next calendar day, same wall-clock time
        candidate = datetime(nxt.year, nxt.month, nxt.day, hour, minute, tzinfo=tz)
    return candidate.astimezone(timezone.utc)


def refund_gate(t: datetime, margin_hours: float | None = None, **kw) -> datetime:
    margin = timedelta(hours=settings.settlement_margin_hours if margin_hours is None else margin_hours)
    return first_cutoff_after(t, **kw) + margin   # both in UTC: margin is elapsed time


def store_time(t: datetime, fmt: str = "%b %-d, %-I:%M %p %Z") -> str:
    return t.astimezone(ZoneInfo(settings.store_timezone)).strftime(fmt)


def effective_config() -> dict:
    """The settlement/refund settings actually in force, and where each came from.
    Logged at startup and shown in /health, so a wrong override (e.g. a stale
    SETTLEMENT_CUTOFF on the host) is visible right after a deploy, not via a refund time."""
    def item(env_key: str, value):
        return {"value": value, "source": "env" if os.environ.get(env_key, "").strip() else "default"}
    return {
        "settlement_cutoff": item("SETTLEMENT_CUTOFF", settings.settlement_cutoff),
        "settlement_timezone": item("SETTLEMENT_TIMEZONE", settings.settlement_timezone),
        "settlement_margin_hours": item("SETTLEMENT_MARGIN_HOURS", settings.settlement_margin_hours),
        "refund_max_auto_attempts_per_payment": item("REFUND_MAX_AUTO_ATTEMPTS_PER_PAYMENT",
                                                     settings.refund_max_auto_attempts_per_payment),
        "refund_same_amount_spacing_seconds": item("REFUND_SAME_AMOUNT_SPACING_SECONDS",
                                                   settings.refund_same_amount_spacing_seconds),
        "store_timezone": item("STORE_TIMEZONE", settings.store_timezone),
    }


def describe_config(cfg: dict | None = None) -> str:
    cfg = cfg or effective_config()
    return ", ".join(f"{k}={v['value']} ({v['source']})" for k, v in cfg.items())
