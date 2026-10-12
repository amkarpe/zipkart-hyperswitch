"""One-time reset of a dev database that was branched from production.

A Neon branch created from production starts with copies of production's orders, which
carry REAL Hyperswitch payment and refund IDs. If anything on dev (a sweep, a refund
retry, a cancel) acted on them, two systems would act on the same sandbox payments, each
with its own attempt count. This removes that copied payment state.

Deletes (everything that hangs off orders, plus copied webhook traffic):
    refunds, remedy_schedule_changes, remedy_txn_corrections, remedies,
    order_items, payments, orders                        (the foreign-key chain)
    webhook_events, webhook_failures                     (no FKs, but copies of production
                                                          deliveries for real payment/refund IDs)
Resets, not deletes:
    sweep_lease                                           (the code needs its single row id=1;
                                                          last_result names production payments)
Keeps:
    products, schema_migrations                           (seed data and schema history)

Refuses to run against production (app/dbguard.py). Prints the target endpoint and row
counts, asks you to type a confirmation, deletes in one transaction, prints counts after.

Usage:  .venv/bin/python -m scripts.reset_dev_data
"""
import os
import sys
from urllib.parse import urlparse

import psycopg

from app.config import settings
from app.dbguard import assert_not_production

DELETE_ORDER = ["refunds", "remedy_schedule_changes", "remedy_txn_corrections", "remedies", "order_items", "payments", "orders", "webhook_events", "webhook_failures"]
RESET = ["sweep_lease"]
KEEP = ["products", "schema_migrations"]


def counts(conn) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
    unknown = set(tables) - set(DELETE_ORDER) - set(RESET) - set(KEEP)
    if unknown:   # a table added after this script was written: decide explicitly, don't guess
        sys.exit(f"Unknown table(s) {sorted(unknown)}: update scripts/reset_dev_data.py first.")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def show(title: str, c: dict[str, int]) -> None:
    print(f"\n{title}")
    for t, n in c.items():
        action = "delete" if t in DELETE_ORDER else "reset" if t in RESET else "keep"
        print(f"  {t:20} {n:6}   ({action})")


def main() -> None:
    assert_not_production(settings.database_url, "the dev reset")
    host = urlparse(settings.database_url).hostname or ""
    endpoint = host.split(".")[0]
    print(f"Target database endpoint: {endpoint}")
    print(f"Production endpoint (PROD_DB_HOST): {(os.environ.get('PROD_DB_HOST') or '(not set)').split('.')[0]}")

    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        show("Before:", counts(conn))
        typed = input(f"\nThis deletes all order and payment data on {endpoint}.\n"
                      f"Type the endpoint id to confirm: ").strip()
        if typed != endpoint:
            sys.exit("Not confirmed; nothing changed.")
        with conn.transaction():
            for t in DELETE_ORDER:
                conn.execute(f"DELETE FROM {t}")
            conn.execute("UPDATE sweep_lease SET locked_until = '-infinity', owner = NULL, "
                         "last_run_at = NULL, last_result = NULL WHERE id = 1")
        show("After:", counts(conn))
    print("\nDone.")


if __name__ == "__main__":
    main()
