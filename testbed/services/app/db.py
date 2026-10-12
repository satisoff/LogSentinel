"""asyncpg pool with latency/timeout instrumentation. Connects via Toxiproxy for fault injection."""

from __future__ import annotations

import asyncio
import os
import time

import asyncpg

from common import DB_LATENCY, DB_POOL_IN_USE, SERVICE, TIMEOUTS, ServiceError, log

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@toxiproxy:5433/app")
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "8"))
QUERY_TIMEOUT_S = float(os.environ.get("DB_QUERY_TIMEOUT_S", "1.0"))
ACQUIRE_TIMEOUT_S = float(os.environ.get("DB_ACQUIRE_TIMEOUT_S", "1.0"))

_pool: asyncpg.Pool | None = None
_in_use = 0


async def connect() -> None:
    global _pool
    for attempt in range(30):
        try:
            _pool = await asyncpg.create_pool(DSN, min_size=2, max_size=POOL_MAX)
            log.info("database pool ready with max size %d", POOL_MAX, extra={"event": "db_ready"})
            return
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("database not ready (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(1)
    raise RuntimeError("database unavailable")


async def close() -> None:
    if _pool is not None:
        await _pool.close()


async def fetch(query_name: str, sql: str, *args):
    global _in_use
    if _pool is None:
        raise ServiceError(503, "database pool not initialised", "DatabaseUnavailable")
    start = time.perf_counter()
    try:
        async with _pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            _in_use += 1
            DB_POOL_IN_USE.labels(SERVICE).set(_in_use)
            try:
                return await conn.fetch(sql, *args, timeout=QUERY_TIMEOUT_S)
            finally:
                _in_use -= 1
                DB_POOL_IN_USE.labels(SERVICE).set(_in_use)
    except TimeoutError:
        TIMEOUTS.labels(SERVICE, "db").inc()
        raise ServiceError(
            504, f"database query {query_name} timed out", "DatabaseTimeout"
        ) from None
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise ServiceError(
            503, f"database error in {query_name}: {type(exc).__name__}", "DatabaseError"
        ) from None
    finally:
        DB_LATENCY.labels(SERVICE, query_name).observe(time.perf_counter() - start)
