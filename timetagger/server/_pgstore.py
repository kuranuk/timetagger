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

    @property
    def mtime(self) -> float:
        return self._mtime if self._mtime is not None else 0.0

    async def _load_mtime(self) -> None:
        assert self._conn is not None
        row = await self._conn.fetchrow(
            "SELECT mtime FROM db_meta WHERE username = $1", self._username
        )
        self._mtime = float(row["mtime"]) if row else 0.0

    async def open(self) -> None:
        pool = await get_pool()
        self._conn = await pool.acquire()
        await self._load_mtime()

    async def close(self) -> None:
        if self._conn is not None:
            pool = await get_pool()
            await pool.release(self._conn)
            self._conn = None

    # -- transaction context manager ------------------------------------------

    async def __aenter__(self):
        assert self._conn is not None, "call open() before entering transaction"
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self._tx.rollback()
        else:
            await self._tx.commit()
        self._tx = None
        return False

    # -- writes ----------------------------------------------------------------

    async def put_one(self, table: str, **fields: Any) -> None:
        """Upsert a single item given as keyword args."""
        await self._upsert(table, fields)

    async def put(self, table: str, item: dict) -> None:
        """Upsert a single item given as a dict."""
        await self._upsert(table, item)

    async def _upsert(self, table: str, item: dict) -> None:
        assert self._conn is not None
        key = item["key"]
        st = float(item["st"])
        mt = int(item["mt"])
        ob = json.dumps(item)

        if table == "records":
            t1 = int(item["t1"])
            t2 = int(item["t2"])
            ds = item.get("ds", "")
            await self._conn.execute(
                f"INSERT INTO {table} (username, key, st, mt, t1, t2, ds, ob) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                f"ON CONFLICT (username, key) DO UPDATE SET "
                f"st=EXCLUDED.st, mt=EXCLUDED.mt, t1=EXCLUDED.t1, "
                f"t2=EXCLUDED.t2, ds=EXCLUDED.ds, ob=EXCLUDED.ob",
                self._username, key, st, mt, t1, t2, ds, ob,
            )
        else:
            await self._conn.execute(
                f"INSERT INTO {table} (username, key, st, mt, ob) "
                f"VALUES ($1, $2, $3, $4, $5) "
                f"ON CONFLICT (username, key) DO UPDATE SET "
                f"st=EXCLUDED.st, mt=EXCLUDED.mt, ob=EXCLUDED.ob",
                self._username, key, st, mt, ob,
            )

        # bump mtime
        await self._conn.execute(
            "INSERT INTO db_meta (username, mtime) VALUES ($1, $2) "
            "ON CONFLICT (username) DO UPDATE SET mtime = GREATEST(db_meta.mtime, EXCLUDED.mtime)",
            self._username, st,
        )

    # -- reads -----------------------------------------------------------------

    async def select_one(self, table: str, where: str, *params) -> Optional[dict]:
        rows = await self.select(table, where, *params)
        return rows[0] if rows else None

    async def select(self, table: str, where: str = "", *params) -> list[dict]:
        assert self._conn is not None
        translated, new_params = _translate_where(where, params, start_param=2)
        if translated:
            sql = f"SELECT ob FROM {table} WHERE username = $1 AND {translated}"
        else:
            sql = f"SELECT ob FROM {table} WHERE username = $1"
        all_params = [self._username] + list(new_params)
        rows = await self._conn.fetch(sql, *all_params)
        return [json.loads(r["ob"]) for r in rows]

    async def select_all(self, table: str) -> list[dict]:
        return await self.select(table)


def _translate_where(where: str, params: tuple, start_param: int = 2) -> tuple[str, list]:
    """Translate itemdb-style WHERE to PostgreSQL.

    - Normalizes ``==`` to ``=``
    - Replaces ``?`` placeholders with ``$N``
    """
    if not where or not where.strip():
        return "", []
    clause = where.replace("==", "=")
    new_params: list = []
    result_parts: list[str] = []
    param_idx = start_param
    p_idx = 0
    for ch in clause:
        if ch == "?":
            result_parts.append(f"${param_idx}")
            new_params.append(params[p_idx])
            param_idx += 1
            p_idx += 1
        else:
            result_parts.append(ch)
    return "".join(result_parts), new_params
