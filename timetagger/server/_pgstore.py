"""Thin asyncpg adapter that mimics itemdb.AsyncItemDB's surface."""
import os
import json
from typing import Any, Optional
import asyncpg

_pool: Optional[asyncpg.Pool] = None


def _dsn() -> str:
    for key in ("TIMETAGGER_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"):
        v = os.environ.get(key)
        if v:
            return v
    raise RuntimeError("No database URL env var set")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_dsn(),
            min_size=0,
            max_size=4,
            max_inactive_connection_lifetime=30,
            statement_cache_size=0,
        )
    return _pool


async def reset_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


class AsyncPgDB:
    def __init__(self, username: str):
        self._username = username
        self._conn: Optional[asyncpg.Connection] = None
        self._tx = None
        self._mtime: Optional[float] = None

    async def open(self) -> None:
        pool = await get_pool()
        self._conn = await pool.acquire()

    async def close(self) -> None:
        if self._conn is not None:
            pool = await get_pool()
            await pool.release(self._conn)
            self._conn = None
