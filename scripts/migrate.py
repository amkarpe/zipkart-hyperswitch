"""Apply migrations/*.sql in filename order, each exactly once.

Usage:  python -m scripts.migrate
Each file runs in its own transaction together with its schema_migrations row,
so a file is either fully applied and recorded, or not applied at all.
"""
from pathlib import Path

import psycopg

from app.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def main() -> None:
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   filename   TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        conn.commit()
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"  skip   {path.name}")
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            print(f"  apply  {path.name}")


if __name__ == "__main__":
    main()
