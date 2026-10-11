"""Apply migrations/*.sql in filename order, each exactly once.

Usage:  python -m scripts.migrate      (Render runs this before starting the app)

Each file runs in its own transaction together with its schema_migrations row, so a
file is either fully applied and recorded, or not applied at all. The transaction
first takes a transaction-scoped advisory lock and re-checks schema_migrations, so
two instances starting at once can't both apply the same file. (Transaction-scoped,
not session-scoped: Neon's PgBouncer pooler doesn't keep a session between statements.)
"""
from pathlib import Path

import psycopg

from app.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_KEY = 0x5A4B_4D49   # "ZKMI": any constant shared by every instance


def main() -> None:
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename   TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                if conn.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (path.name,)).fetchone():
                    print(f"  skip   {path.name}")
                    continue
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            print(f"  apply  {path.name}")


if __name__ == "__main__":
    main()
