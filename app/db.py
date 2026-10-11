"""Postgres connection pool (psycopg 3, plain SQL).

prepare_threshold=None: DATABASE_URL points at Neon's PgBouncer pooler in
transaction mode, where server-side prepared statements are not safe because
consecutive transactions can land on different backend connections.
"""
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

pool = AsyncConnectionPool(
    conninfo=settings.database_url,
    min_size=1,
    max_size=5,
    # Neon suspends after ~5 min idle, so a pooled connection can be dead when
    # we pick it up. check_connection pings it before handing it out.
    check=AsyncConnectionPool.check_connection,
    max_idle=120,
    kwargs={"prepare_threshold": None, "row_factory": dict_row},
    open=False,
)


async def open_pool() -> None:
    await pool.open(wait=True, timeout=30)


async def close_pool() -> None:
    await pool.close()
