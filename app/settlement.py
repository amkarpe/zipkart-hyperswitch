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
