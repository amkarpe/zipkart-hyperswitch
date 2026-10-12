"""Keep local tools off the production database.

Migrations reach production only through a deploy (Render runs scripts/migrate.py at
start). Locally, tests and scripts/migrate.py must point at a Neon dev branch. This
guard refuses to run them when DATABASE_URL points at the production host.

PROD_DB_HOST comes from the environment (.env locally), never hardcoded. Locally the
guard fails closed: if PROD_DB_HOST isn't set, it refuses, because it can't tell.
On Render (RENDER=true, set by Render on every service) it allows the run: that is
the deploy path.
"""
import os
from urllib.parse import urlparse


class ProductionDatabaseError(RuntimeError):
    pass


def _endpoint(host: str) -> str:
    # Neon hosts: ep-xxx-pooler.region.aws.neon.tech (pooled) / ep-xxx.region... (direct).
    # Both reach the same compute, so compare the endpoint id without "-pooler".
    first, _, rest = host.lower().partition(".")
    return f"{first.removesuffix('-pooler')}.{rest}"


def assert_not_production(database_url: str, purpose: str) -> None:
    if os.environ.get("RENDER") == "true":
        return
    prod = os.environ.get("PROD_DB_HOST", "").strip()
    if not prod:
        raise ProductionDatabaseError(
            f"Refusing to run {purpose}: PROD_DB_HOST isn't set, so it can't be checked that "
            "DATABASE_URL isn't production. Set PROD_DB_HOST in .env to the production database host.")
    host = urlparse(database_url).hostname or ""
    if _endpoint(host) == _endpoint(prod):
        raise ProductionDatabaseError(
            f"Refusing to run {purpose} against the production database ({host}). Point DATABASE_URL at the "
            "Neon dev branch; migrations reach production only through a deploy.")
