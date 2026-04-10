# TimeTagger on Vercel + Supabase — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port TimeTagger to run on Vercel Functions (Python, Fluid Compute) with Supabase Postgres as the data store, keeping the existing bcrypt credential auth and client unchanged.

**Architecture:** Replace per-user SQLite (`itemdb`) with a thin `asyncpg` adapter against one shared Supabase Postgres schema partitioned by `username`. Pre-compile all client assets to `public/` at build time so the Python Function only handles `/api/v2/*`. Keep the diff in `_apiserver.py` minimal by matching the `itemdb` method shape. Enable RLS (no policies) on every table so only the service-role Postgres connection used by the Function can touch the data.

**Tech Stack:** Python 3.13, `asgineer`, `asyncpg`, `pscript`, `pyjwt`, `bcrypt`, Supabase Postgres (Supavisor pooled endpoint on port 6543), Vercel Fluid Compute.

**Design doc:** `docs/plans/2026-04-10-vercel-supabase-port-design.md`

---

## Preflight

Before starting Task 1, read:

- `docs/plans/2026-04-10-vercel-supabase-port-design.md` (the approved design)
- `timetagger/server/_apiserver.py` (every call to `itemdb` you'll touch)
- `timetagger/server/_utils.py` (JWT key loading)
- `timetagger/__main__.py` (ASGI entry you'll slim down into `api/index.py`)
- `tests/` (whatever is there — we'll adapt it)
- `supabase/migrations/20260410000001_init.sql` (already created — the SQL
  you'll be applying)

Ensure a local Postgres 15 is available for testing (Supabase ships Postgres
15; use the same to avoid subtle behavior differences):

```bash
docker run --rm -d --name timetagger-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:15
export DATABASE_URL=postgres://postgres:postgres@localhost:5432/postgres
```

For end-to-end verification against real Supabase:

```bash
# Option A: Supabase CLI (spins up a local stack)
supabase start
# Uses the connection strings it prints.

# Option B: A Supabase project in the cloud
vercel env pull .env.local --yes  # pulls POSTGRES_URL / POSTGRES_URL_NON_POOLING
```

---

## Task 1: Add `asyncpg`, drop `itemdb` from deps

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt` (if missing, for test-only deps)

**Step 1: Edit `requirements.txt`**

Remove `itemdb>=1.1.1` and add `asyncpg>=0.29`.

Before:
```
uvicorn
asgineer>=0.8.0
itemdb>=1.1.1
pscript>=0.7.6
pyjwt
jinja2
markdown
bcrypt
iptools
```

After:
```
uvicorn
asgineer>=0.8.0
asyncpg>=0.29
pscript>=0.7.6
pyjwt
jinja2
markdown
bcrypt
iptools
```

**Step 2: Install locally to verify**

```bash
pip install -r requirements.txt
```
Expected: clean install, no errors.

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: swap itemdb for asyncpg"
```

---

## Task 2: Migration runner (init SQL is already in place)

The initial schema is already written to
`supabase/migrations/20260410000001_init.sql` (Supabase CLI filename
convention: `YYYYMMDDHHMMSS_<name>.sql`). This task wires up the Python runner
that reads that directory, so CI and non-CLI environments have a zero-
dependency path to apply the schema.

**Files:**
- Reference (already exists): `supabase/migrations/20260410000001_init.sql`
- Create: `scripts/migrate.py`
- Create: `scripts/__init__.py` (empty)
- Create: `tests/test_migrate.py`

**Step 1: Write the failing test `tests/test_migrate.py`**

```python
import os
import asyncio
import asyncpg
import pytest
from scripts import migrate

DATABASE_URL = os.environ["DATABASE_URL"]

@pytest.fixture
async def clean_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    yield

@pytest.mark.asyncio
async def test_migrate_creates_tables(clean_db):
    await migrate.run(DATABASE_URL)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        tables = {
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
    finally:
        await conn.close()

    assert {"records", "settings", "userinfo", "db_meta", "schema_migrations"} <= tables

@pytest.mark.asyncio
async def test_migrate_is_idempotent(clean_db):
    await migrate.run(DATABASE_URL)
    await migrate.run(DATABASE_URL)  # should not raise
```

**Step 2: Run test, expect failure**

```bash
pip install pytest pytest-asyncio
pytest tests/test_migrate.py -v
```
Expected: ImportError (`scripts.migrate` not found) or similar.

**Step 3: Write `scripts/migrate.py`**

```python
"""Apply SQL files in supabase/migrations/ in order, track in schema_migrations.

Reads from the Supabase CLI convention directory so that the same files work
with `supabase db push` (primary, CLI-driven) and this Python runner (fallback,
used by CI and ad-hoc remote runs). Lexicographic sort matches Supabase's
`YYYYMMDDHHMMSS_<name>.sql` naming.

Prefers POSTGRES_URL_NON_POOLING (port 5432, direct) over the pooled endpoint,
because Supavisor transaction-mode does not support all DDL / advisory locks
we rely on during migration.
"""
import asyncio
import os
import sys
from pathlib import Path
import asyncpg

MIG_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _dsn() -> str:
    for key in (
        "TIMETAGGER_DATABASE_URL",
        "POSTGRES_URL_NON_POOLING",
        "DATABASE_URL",
        "POSTGRES_URL",
    ):
        v = os.environ.get(key)
        if v:
            return v
    raise RuntimeError(
        "Set POSTGRES_URL_NON_POOLING (Supabase direct) or DATABASE_URL"
    )


async def run(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA_TABLE)
        applied = {
            r["version"]
            for r in await conn.fetch("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIG_DIR.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            print(f"applied {version}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run(_dsn()))
```

Also create `scripts/__init__.py` (empty) so pytest can import it.

**Step 4: Run tests, expect pass**

```bash
pytest tests/test_migrate.py -v
```
Expected: 2 passed.

**Step 5: Commit**

```bash
git add supabase/ scripts/migrate.py scripts/__init__.py tests/test_migrate.py
git commit -m "feat: supabase migrations directory + python runner"
```

---

## Task 3: `_pgstore.py` — pool + connection lifecycle

We'll build the adapter incrementally with TDD. This task sets up the pool and `open()`/`close()`.

**Files:**
- Create: `timetagger/server/_pgstore.py`
- Create: `tests/test_pgstore.py`

**Step 1: Write the failing test**

```python
import os
import pytest
from timetagger.server._pgstore import AsyncPgDB, get_pool, reset_pool
from scripts import migrate

DATABASE_URL = os.environ["DATABASE_URL"]

@pytest.fixture(autouse=True)
async def fresh_schema():
    import asyncpg
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await reset_pool()
    await migrate.run(DATABASE_URL)
    yield
    await reset_pool()

@pytest.mark.asyncio
async def test_open_and_close_acquires_releases():
    os.environ["TIMETAGGER_DATABASE_URL"] = DATABASE_URL
    db = AsyncPgDB("alice")
    await db.open()
    assert db._conn is not None
    await db.close()
    assert db._conn is None
```

**Step 2: Run, expect failure**

```bash
pytest tests/test_pgstore.py::test_open_and_close_acquires_releases -v
```
Expected: ImportError.

**Step 3: Write minimal `_pgstore.py`**

```python
"""Thin asyncpg adapter that mimics itemdb.AsyncItemDB's surface."""
import os
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
```

**Step 4: Run test, expect pass**

```bash
pytest tests/test_pgstore.py -v
```
Expected: 1 passed.

**Step 5: Commit**

```bash
git add timetagger/server/_pgstore.py tests/test_pgstore.py
git commit -m "feat(pgstore): pool + open/close lifecycle"
```

---

## Task 4: `_pgstore.py` — `put_one` / `select_one` basics

**Files:**
- Modify: `timetagger/server/_pgstore.py`
- Modify: `tests/test_pgstore.py`

**Step 1: Add failing tests**

Append to `tests/test_pgstore.py`:

```python
@pytest.mark.asyncio
async def test_put_one_then_select_one_userinfo():
    db = AsyncPgDB("alice")
    await db.open()
    try:
        async with db:
            await db.put_one("userinfo", key="reset_time", st=1.0, mt=1.0, value=42.0)
        row = await db.select_one("userinfo", "key == 'reset_time'")
    finally:
        await db.close()
    assert row is not None
    assert row["value"] == 42.0
    assert row["key"] == "reset_time"

@pytest.mark.asyncio
async def test_select_one_missing_returns_none():
    db = AsyncPgDB("alice")
    await db.open()
    try:
        row = await db.select_one("userinfo", "key == 'nope'")
    finally:
        await db.close()
    assert row is None

@pytest.mark.asyncio
async def test_users_isolated():
    a = AsyncPgDB("alice"); await a.open()
    b = AsyncPgDB("bob");   await b.open()
    try:
        async with a:
            await a.put_one("userinfo", key="x", st=1, mt=1, value="A")
        async with b:
            await b.put_one("userinfo", key="x", st=1, mt=1, value="B")
        ra = await a.select_one("userinfo", "key == 'x'")
        rb = await b.select_one("userinfo", "key == 'x'")
    finally:
        await a.close(); await b.close()
    assert ra["value"] == "A"
    assert rb["value"] == "B"
```

**Step 2: Run, expect failure**

```bash
pytest tests/test_pgstore.py -v
```
Expected: AttributeError on `put_one` / `select_one` / `__aenter__`.

**Step 3: Extend `_pgstore.py`**

Add to the `AsyncPgDB` class:

```python
import json

    # ---- transactions ----
    async def __aenter__(self):
        if self._conn is None:
            await self.open()
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            await self._tx.rollback()
        else:
            await self._tx.commit()
        self._tx = None

    # ---- writes ----
    _TABLES_WITH_T = {"records"}  # table -> has t1/t2/ds columns

    async def put_one(self, table: str, **fields: Any) -> None:
        await self._upsert(table, fields)

    async def put(self, table: str, item: dict) -> None:
        await self._upsert(table, dict(item))

    async def _upsert(self, table: str, item: dict) -> None:
        assert self._conn is not None
        username = self._username
        key = item["key"]
        st = float(item["st"])
        mt = int(item["mt"])
        ob = dict(item)  # full payload goes into JSONB
        if table == "records":
            t1 = int(item["t1"])
            t2 = int(item["t2"])
            ds = item.get("ds")
            sql = """
                INSERT INTO records (username, key, st, mt, t1, t2, ds, ob)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (username, key) DO UPDATE SET
                  st = EXCLUDED.st, mt = EXCLUDED.mt,
                  t1 = EXCLUDED.t1, t2 = EXCLUDED.t2,
                  ds = EXCLUDED.ds, ob = EXCLUDED.ob
            """
            await self._conn.execute(
                sql, username, key, st, mt, t1, t2, ds, json.dumps(ob)
            )
        else:
            sql = f"""
                INSERT INTO {table} (username, key, st, mt, ob)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (username, key) DO UPDATE SET
                  st = EXCLUDED.st, mt = EXCLUDED.mt, ob = EXCLUDED.ob
            """
            await self._conn.execute(sql, username, key, st, mt, json.dumps(ob))
        # bump db_meta.mtime
        await self._conn.execute(
            """
            INSERT INTO db_meta (username, mtime) VALUES ($1, $2)
            ON CONFLICT (username) DO UPDATE SET mtime = EXCLUDED.mtime
            """,
            username, st,
        )
        self._mtime = st

    # ---- reads ----
    async def select_one(self, table: str, where: str, *params: Any) -> Optional[dict]:
        rows = await self._select(table, where, params, limit=1)
        return rows[0] if rows else None

    async def select(self, table: str, where: str, *params: Any) -> list[dict]:
        return await self._select(table, where, params, limit=None)

    async def select_all(self, table: str) -> list[dict]:
        return await self._select(table, "", (), limit=None)

    async def _select(
        self, table: str, where: str, params: tuple, limit: Optional[int]
    ) -> list[dict]:
        assert self._conn is not None
        sql_where, sql_params = _translate_where(where, params, start_param=2)
        full_where = f"username = $1{' AND (' + sql_where + ')' if sql_where else ''}"
        sql = f"SELECT ob FROM {table} WHERE {full_where}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = await self._conn.fetch(sql, self._username, *sql_params)
        return [json.loads(r["ob"]) for r in rows]


def _translate_where(where: str, params: tuple, start_param: int) -> tuple[str, tuple]:
    """Translate itemdb's SQLite-ish WHERE to asyncpg's $N placeholders.

    Supported shapes (from _apiserver.py):
      - "" (empty → select all)
      - "key == 'foo'"  or  "key = 'foo'"
      - "key == ?"       → key = $N
      - "st >= <number>" → literal kept as-is, numbers are safe
      - "<expr> AND <expr>" (combinations of the above)
    For anything more exotic the caller should use an explicit SQL path.
    """
    if not where.strip():
        return "", ()
    # Normalize '==' to '='
    translated = where.replace("==", "=")
    # Replace '?' placeholders with $N, consuming params left-to-right
    out_parts: list[str] = []
    remaining = translated
    i = start_param
    used = 0
    while True:
        idx = remaining.find("?")
        if idx == -1:
            out_parts.append(remaining)
            break
        out_parts.append(remaining[:idx])
        out_parts.append(f"${i}")
        i += 1
        used += 1
        remaining = remaining[idx + 1 :]
    return "".join(out_parts), params[:used]
```

**Step 4: Run tests, expect pass**

```bash
pytest tests/test_pgstore.py -v
```
Expected: 4 passed.

**Step 5: Commit**

```bash
git add timetagger/server/_pgstore.py tests/test_pgstore.py
git commit -m "feat(pgstore): put/select basics + where translation"
```

---

## Task 5: `_pgstore.py` — records-specific selects and `mtime`

**Files:**
- Modify: `timetagger/server/_pgstore.py`
- Modify: `tests/test_pgstore.py`

**Step 1: Add failing tests**

```python
@pytest.mark.asyncio
async def test_records_select_by_timerange():
    db = AsyncPgDB("alice"); await db.open()
    try:
        async with db:
            for i, (t1, t2) in enumerate([(10, 20), (30, 40), (50, 60)]):
                await db.put("records", {
                    "key": f"r{i}", "st": float(t1), "mt": t1,
                    "t1": t1, "t2": t2, "ds": f"#tag{i}",
                })
        rows = await db.select("records", "(t2 >= 25 AND t1 <= 45)")
    finally:
        await db.close()
    keys = sorted(r["key"] for r in rows)
    assert keys == ["r1"]

@pytest.mark.asyncio
async def test_records_select_by_ds_like():
    db = AsyncPgDB("alice"); await db.open()
    try:
        async with db:
            await db.put("records", {
                "key": "r1", "st": 1.0, "mt": 1,
                "t1": 1, "t2": 2, "ds": "#work meeting",
            })
            await db.put("records", {
                "key": "r2", "st": 1.0, "mt": 1,
                "t1": 1, "t2": 2, "ds": "#home cooking",
            })
        rows = await db.select("records", "ds LIKE ? ESCAPE '\\'", "%#work %")
    finally:
        await db.close()
    assert len(rows) == 1 and rows[0]["key"] == "r1"

@pytest.mark.asyncio
async def test_mtime_increases_on_write():
    db = AsyncPgDB("alice"); await db.open()
    try:
        await db._load_mtime()
        t0 = db.mtime
        async with db:
            await db.put_one("userinfo", key="x", st=t0 + 100.0, mt=1, value=1)
        await db._load_mtime()
        assert db.mtime >= t0 + 100.0
    finally:
        await db.close()
```

**Step 2: Run, expect failure**

```bash
pytest tests/test_pgstore.py::test_records_select_by_timerange tests/test_pgstore.py::test_records_select_by_ds_like tests/test_pgstore.py::test_mtime_increases_on_write -v
```
Expected: failures (the select on records should already work, mtime attr missing).

**Step 3: Add mtime + ensure records query translation handles real columns**

Add to `AsyncPgDB`:

```python
    @property
    def mtime(self) -> float:
        return self._mtime if self._mtime is not None else 0.0

    async def _load_mtime(self) -> None:
        assert self._conn is not None
        row = await self._conn.fetchrow(
            "SELECT mtime FROM db_meta WHERE username = $1", self._username
        )
        self._mtime = float(row["mtime"]) if row else 0.0
```

Modify `open()` to load mtime:

```python
    async def open(self) -> None:
        pool = await get_pool()
        self._conn = await pool.acquire()
        await self._load_mtime()
```

`_translate_where` already passes through literals like `(t2 >= 25 AND t1 <= 45)` and `ds LIKE ? ESCAPE '\\'` because they are referring to real columns. Verify the tests pass.

**Step 4: Run tests**

```bash
pytest tests/test_pgstore.py -v
```
Expected: all tests pass.

**Step 5: Commit**

```bash
git add timetagger/server/_pgstore.py tests/test_pgstore.py
git commit -m "feat(pgstore): mtime tracking + records range/ds queries"
```

---

## Task 6: JWT key from env var

**Files:**
- Modify: `timetagger/server/_utils.py:55-73`
- Create: `tests/test_jwt_key.py`

**Step 1: Write failing test**

```python
import importlib
import os

def test_jwt_key_from_env(monkeypatch):
    monkeypatch.setenv("TIMETAGGER_JWT_SECRET", "my-test-secret-1234567890")
    from timetagger.server import _utils
    importlib.reload(_utils)
    assert _utils.JWT_KEY == "my-test-secret-1234567890"
```

**Step 2: Run**

```bash
pytest tests/test_jwt_key.py -v
```
Expected: fail (current impl reads file regardless).

**Step 3: Patch `_utils.py:55-73`**

Replace `_load_jwt_key`:

```python
def _load_jwt_key():
    """Load the JWT signing key.

    Preference order:
      1. TIMETAGGER_JWT_SECRET env var (required on Vercel; fs is ephemeral).
      2. A local file under ROOT_TT_DIR/jwt.key (dev convenience). Generated on
         first use.
    """
    secret = os.environ.get("TIMETAGGER_JWT_SECRET", "").strip()
    if secret:
        return secret
    filename = os.path.join(ROOT_TT_DIR, "jwt.key")
    secret = ""
    if os.path.isfile(filename):
        with open(filename, "rb") as f:
            secret = f.read().decode().strip()
    if not secret:
        secret = secrets.token_urlsafe(32)
        os.makedirs(ROOT_TT_DIR, exist_ok=True)
        with open(filename, "wb") as f:
            f.write(secret.encode())
    return secret
```

(The only behavioral change is the env-var check at the top, plus `makedirs` for robustness on first run.)

**Step 4: Run**

```bash
pytest tests/test_jwt_key.py -v
```
Expected: pass.

**Step 5: Commit**

```bash
git add timetagger/server/_utils.py tests/test_jwt_key.py
git commit -m "feat(auth): allow JWT key from env var for stateless deploy"
```

---

## Task 7: Swap `itemdb` → `AsyncPgDB` in `_apiserver.py`

**Files:**
- Modify: `timetagger/server/_apiserver.py:10,188-192,286-288,404-424`

**Step 1: Rewrite `authenticate()`**

At `_apiserver.py:10`, remove `import itemdb`. Add:

```python
from ._pgstore import AsyncPgDB
```

Remove `user2filename` from the import at line 12 (leave `create_jwt, decode_jwt`).

At `_apiserver.py:187-192`, replace:

```python
    # Open the database, this creates it if it does not yet exist
    dbname = user2filename(auth_info["username"])
    db = await itemdb.AsyncItemDB(dbname)
    await db.ensure_table("userinfo", *INDICES["userinfo"])
    await db.ensure_table("records", *INDICES["records"])
    await db.ensure_table("settings", *INDICES["settings"])
```

with:

```python
    # Open a DB handle for this user (schema is managed by migrations/)
    db = AsyncPgDB(auth_info["username"])
    await db.open()
```

At `_apiserver.py:286-288` (inside `get_webtoken_unsafe`), replace:

```python
    dbname = user2filename(username)
    db = await itemdb.AsyncItemDB(dbname)
    await db.ensure_table("userinfo", *INDICES["userinfo"])
```

with:

```python
    db = AsyncPgDB(username)
    await db.open()
```

…and add a `finally: await db.close()` around the token logic so the connection is returned to the pool.

**Step 2: Rewrite `get_records` compound query**

At `_apiserver.py:404-424`, replace the `json_extract(_ob, '$.ds')` references with the real `ds` column:

Before:
```python
        query_parts.append(
            "json_extract(_ob, '$.ds') LIKE ? ESCAPE '\\' OR json_extract(_ob, '$.ds') LIKE ? ESCAPE '\\'"
        )
```
After:
```python
        query_parts.append(
            "ds LIKE ? ESCAPE '\\' OR ds LIKE ? ESCAPE '\\'"
        )
```

And:

Before:
```python
    if hidden is True:
        query_parts.append("json_extract(_ob, '$.ds') LIKE 'HIDDEN%'")
    if hidden is False:
        query_parts.append("json_extract(_ob, '$.ds') NOT LIKE 'HIDDEN%'")
```
After:
```python
    if hidden is True:
        query_parts.append("ds LIKE 'HIDDEN%'")
    if hidden is False:
        query_parts.append("ds NOT LIKE 'HIDDEN%'")
```

**Step 3: Run existing server module import to sanity-check**

```bash
python -c "from timetagger.server import _apiserver; print('ok')"
```
Expected: `ok`.

**Step 4: Commit**

```bash
git add timetagger/server/_apiserver.py
git commit -m "refactor(api): use AsyncPgDB in place of itemdb"
```

---

## Task 8: End-to-end API integration test against Postgres

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_api_integration.py`

**Step 1: Write a minimal ASGI test harness**

`tests/conftest.py`:

```python
import os
import asyncio
import pytest
from scripts import migrate
from timetagger.server._pgstore import reset_pool
import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]

@pytest.fixture(autouse=True)
async def _fresh_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await reset_pool()
    os.environ["TIMETAGGER_DATABASE_URL"] = DATABASE_URL
    await migrate.run(DATABASE_URL)
    yield
    await reset_pool()
```

`tests/test_api_integration.py`:

```python
import time
import pytest
from timetagger.server._pgstore import AsyncPgDB
from timetagger.server._apiserver import put_records, get_records

class FakeRequest:
    def __init__(self, method="GET", qs=None, body=None):
        self.method = method
        self.querydict = qs or {}
        self._body = body
    async def get_json(self, limit):
        return self._body

@pytest.mark.asyncio
async def test_put_and_get_records_roundtrip():
    db = AsyncPgDB("alice"); await db.open()
    try:
        put_req = FakeRequest(
            method="PUT",
            body=[{"key": "r1", "mt": 1, "t1": 10, "t2": 20, "ds": "#work"}],
        )
        status, _, body = await put_records(put_req, {"username": "alice"}, db)
        assert status == 200
        assert body["accepted"] == ["r1"]

        get_req = FakeRequest(qs={"timerange": "0-100"})
        status, _, body = await get_records(get_req, {"username": "alice"}, db)
        assert status == 200
        assert len(body["records"]) == 1
        assert body["records"][0]["ds"] == "#work"
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_get_records_filtered_by_tag():
    db = AsyncPgDB("alice"); await db.open()
    try:
        put = FakeRequest(
            method="PUT",
            body=[
                {"key": "r1", "mt": 1, "t1": 10, "t2": 20, "ds": "#work done"},
                {"key": "r2", "mt": 1, "t1": 10, "t2": 20, "ds": "#home idle"},
            ],
        )
        await put_records(put, {"username": "alice"}, db)

        req = FakeRequest(qs={"timerange": "0-100", "tag": "work"})
        status, _, body = await get_records(req, {"username": "alice"}, db)
        assert status == 200
        keys = [r["key"] for r in body["records"]]
        assert keys == ["r1"]
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_two_users_are_isolated():
    a = AsyncPgDB("alice"); await a.open()
    b = AsyncPgDB("bob");   await b.open()
    try:
        await put_records(
            FakeRequest(method="PUT", body=[
                {"key": "x", "mt": 1, "t1": 1, "t2": 2, "ds": "#a"},
            ]),
            {"username": "alice"}, a,
        )
        await put_records(
            FakeRequest(method="PUT", body=[
                {"key": "x", "mt": 1, "t1": 1, "t2": 2, "ds": "#b"},
            ]),
            {"username": "bob"}, b,
        )
        _, _, body_a = await get_records(
            FakeRequest(qs={"timerange": "0-100"}),
            {"username": "alice"}, a,
        )
        _, _, body_b = await get_records(
            FakeRequest(qs={"timerange": "0-100"}),
            {"username": "bob"}, b,
        )
    finally:
        await a.close(); await b.close()

    assert body_a["records"][0]["ds"] == "#a"
    assert body_b["records"][0]["ds"] == "#b"
```

**Step 2: Run**

```bash
pytest tests/test_api_integration.py -v
```
Expected: 3 passed. Fix any translation gaps in `_pgstore` revealed by these tests before moving on.

**Step 3: Commit**

```bash
git add tests/conftest.py tests/test_api_integration.py
git commit -m "test(api): end-to-end put/get/isolation against postgres"
```

---

## Task 9: `scripts/build_assets.py` — emit `public/`

**Files:**
- Create: `scripts/build_assets.py`
- Modify: `.gitignore` (add `public/`)
- Create: `tests/test_build_assets.py`

**Step 1: Write failing test**

```python
import subprocess
import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def test_build_assets_writes_public(tmp_path, monkeypatch):
    out = tmp_path / "public"
    env = os.environ.copy()
    env["BUILD_OUTPUT_DIR"] = str(out)
    env["TIMETAGGER_PATH_PREFIX"] = "/"
    res = subprocess.run(
        ["python", "scripts/build_assets.py"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert (out / "app").is_dir()
    # at least one HTML and one JS file should land somewhere
    files = list(out.rglob("*"))
    assert any(f.suffix == ".html" for f in files)
    assert any(f.suffix == ".js" for f in files)
    # service worker in app/
    assert any(f.name == "sw.js" for f in (out / "app").iterdir())
```

**Step 2: Run**

```bash
pytest tests/test_build_assets.py -v
```
Expected: fail (script not found).

**Step 3: Write `scripts/build_assets.py`**

```python
"""Pre-compile TimeTagger client assets into a directory for static hosting."""
import os
import sys
from importlib import resources
from pathlib import Path

os.environ.setdefault("TIMETAGGER_PATH_PREFIX", "/")

from timetagger.server import create_assets_from_dir, enable_service_worker  # noqa: E402


def write_assets(out_dir: Path, assets: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content)
        elif isinstance(content, tuple):
            # asgineer may wrap as (content_type, bytes_or_str)
            _ct, body = content
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(body)
        else:
            raise TypeError(f"Unknown asset type for {name}: {type(content)}")


def main() -> int:
    out = Path(os.environ.get("BUILD_OUTPUT_DIR", "public")).resolve()
    if out.exists():
        import shutil
        shutil.rmtree(out)

    common = create_assets_from_dir(resources.files("timetagger.common"))
    apponly = create_assets_from_dir(resources.files("timetagger.app"))
    image = create_assets_from_dir(resources.files("timetagger.images"))
    page = create_assets_from_dir(resources.files("timetagger.pages"))

    app_assets = {**common, **image, **apponly}
    web_assets = {**common, **image, **page}
    enable_service_worker(app_assets)

    write_assets(out / "app", app_assets)
    write_assets(out, web_assets)
    print(f"wrote {sum(1 for _ in out.rglob('*') if _.is_file())} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The `isinstance(content, tuple)` branch is defensive: if `create_assets_from_dir` returns `(content_type, body)` pairs (asgineer convention), handle it. Inspect the return type first and delete the branch you don't need before committing.

**Step 4: Verify the asset dict shape first**

```bash
python -c "
from importlib import resources
from timetagger.server import create_assets_from_dir
d = create_assets_from_dir(resources.files('timetagger.common'))
for k, v in list(d.items())[:3]:
    print(k, type(v).__name__)
"
```
Then remove the unused branch.

**Step 5: Run the test**

```bash
pytest tests/test_build_assets.py -v
```
Expected: pass.

**Step 6: Add `public/` to .gitignore**

Append `public/` to `.gitignore` (create the file if it does not exist).

**Step 7: Commit**

```bash
git add scripts/build_assets.py tests/test_build_assets.py .gitignore
git commit -m "feat(build): pre-compile client assets into public/"
```

---

## Task 10: `api/index.py` — API-only ASGI entry

**Files:**
- Create: `api/index.py`

**Step 1: Write the file**

```python
"""Vercel Python Function: TimeTagger API only.

Static assets live in /public and are served by Vercel Edge Network; vercel.ts
rewrites /api/v2/* to this function.
"""
import asgineer

from timetagger.server import authenticate, AuthException, api_handler_triage
from timetagger.__main__ import get_webtoken


@asgineer.to_asgi
async def app(request):
    path = request.path

    if path == "/api/v2/bootstrap_authentication":
        return await get_webtoken(request)

    if not path.startswith("/api/v2/"):
        return 404, {}, "not found"

    subpath = path.removeprefix("/api/v2/").strip("/")

    try:
        auth_info, db = await authenticate(request)
    except AuthException as err:
        return 401, {}, f"unauthorized: {err}"

    try:
        return await api_handler_triage(request, subpath, auth_info, db)
    finally:
        await db.close()
```

**Step 2: Smoke-import**

```bash
python -c "import api.index; print(api.index.app)"
```
Expected: prints an ASGI callable, no errors.

**Step 3: Commit**

```bash
git add api/index.py
git commit -m "feat(vercel): api function entry"
```

---

## Task 11: `vercel.ts` + `.vercelignore`

**Files:**
- Create: `vercel.ts`
- Create: `.vercelignore`
- Modify: `package.json` (create with minimal contents if missing — Vercel needs it to detect the project and resolve `@vercel/config`)

**Step 1: Create `package.json`**

```json
{
  "name": "timetagger-vercel",
  "private": true,
  "devDependencies": {
    "@vercel/config": "^1.0.0"
  }
}
```

Run `npm install` to lockfile.

**Step 2: Create `vercel.ts`**

```ts
import { routes, type VercelConfig } from '@vercel/config/v1';

export const config: VercelConfig = {
  buildCommand: 'python scripts/build_assets.py',
  outputDirectory: 'public',
  rewrites: [
    routes.rewrite('/api/v2/(.*)', '/api/index'),
  ],
  headers: [
    routes.cacheControl('/app/sw.js', { public: true, maxAge: 0 }),
    routes.cacheControl('/app/(.*)\\.(js|css|woff2|png|svg)', {
      public: true,
      maxAge: '1 year',
      immutable: true,
    }),
  ],
};
```

**Step 3: Create `.vercelignore`**

```
tests/
docs/
deploy/
.github/
```

**Step 4: Commit**

```bash
git add package.json package-lock.json vercel.ts .vercelignore
git commit -m "feat(vercel): project config + build command"
```

---

## Task 12: CI — Postgres service + run new test suites

**Files:**
- Modify: `.github/workflows/ci.yml` (inspect the existing one and adapt)

**Step 1: Inspect current CI**

```bash
cat .github/workflows/*.yml
```

**Step 2: Add a `services: postgres:15` block and a test job that exports `DATABASE_URL`**

Use `postgres:15` to match the Supabase platform version so we exercise the
same behaviors in CI.

```yaml
  test-postgres:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: postgres
        ports: ['5432:5432']
        options: >-
          --health-cmd="pg_isready -U postgres"
          --health-interval=5s
          --health-timeout=3s
          --health-retries=10
    env:
      DATABASE_URL: postgres://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: pip install -r requirements.txt pytest pytest-asyncio
      - run: pytest tests/ -v
```

**Step 3: Delete obsolete SQLite-only test fixtures**

Look for any `test_*` that references `itemdb` directly and remove them.

**Step 4: Commit**

```bash
git add .github/workflows/ tests/
git commit -m "ci: run test suite against postgres 15"
```

---

## Task 13: README section + deploy checklist

**Files:**
- Modify: `README.md`

**Step 1: Append a new section**

````markdown
## Deploying to Vercel + Supabase

TimeTagger can be deployed to Vercel as a Python Function backed by Supabase
Postgres. See `docs/plans/2026-04-10-vercel-supabase-port-design.md` for the
full design.

Quick start:

1. Install the Supabase integration from the Vercel Marketplace
   (auto-provisions `POSTGRES_URL`, `POSTGRES_URL_NON_POOLING`, and
   `SUPABASE_*` variables):

   ```
   vercel integration add supabase
   ```

2. Set the TimeTagger-specific env vars:

   ```
   vercel env add TIMETAGGER_CREDENTIALS    # user1:bcrypt-hash,user2:bcrypt-hash
   vercel env add TIMETAGGER_JWT_SECRET     # 32 random bytes
   vercel env add TIMETAGGER_PATH_PREFIX    # /
   ```

3. Apply the schema once. Either use the Supabase CLI:

   ```
   supabase link --project-ref <your-project-ref>
   supabase db push
   ```

   or the bundled Python runner (reads `supabase/migrations/`):

   ```
   vercel env pull .env.local --yes
   env $(grep POSTGRES_URL_NON_POOLING .env.local) python scripts/migrate.py
   ```

4. Deploy:

   ```
   vercel deploy --prod
   ```
````

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: vercel + supabase deployment section"
```

---

## Task 14: Local smoke test end-to-end

**Step 1: Start Postgres and migrate**

```bash
docker run --rm -d --name timetagger-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:15
export DATABASE_URL=postgres://postgres:postgres@localhost:5432/postgres
python scripts/migrate.py
```
Expected: `applied 20260410000001_init`.

**Step 2: Build assets**

```bash
python scripts/build_assets.py
```
Expected: files written to `public/`, including `public/app/sw.js`.

**Step 3: Run the API locally with uvicorn**

```bash
export TIMETAGGER_JWT_SECRET=testsecretlong1234567890abcdef
export TIMETAGGER_CREDENTIALS='test:$2a$08$0CD1NFiIbancwWsu3se1v.RNR/b7YeZd71yg3cZ/3whGlyU6Iny5i'
uvicorn api.index:app --port 8080
```

**Step 4: Exercise it**

In another shell:

```bash
# Login
TOKEN=$(curl -s -X POST http://localhost:8080/api/v2/bootstrap_authentication \
  --data-binary "$(printf '{"method":"usernamepassword","username":"test","password":"test"}' | base64)" \
  | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')
echo "$TOKEN"

# Push a record
curl -s -X PUT http://localhost:8080/api/v2/records \
  -H "authtoken: $TOKEN" \
  -d '[{"key":"r1","mt":1000,"t1":10,"t2":20,"ds":"#smoke"}]'

# Fetch it back
curl -s "http://localhost:8080/api/v2/records?timerange=0-100" -H "authtoken: $TOKEN"
```
Expected: last call returns JSON containing `r1` with `ds == "#smoke"`.

**Step 5: Tear down**

```bash
docker stop timetagger-pg
```

No commit — this is a manual verification gate.

---

## Task 15: Final sanity pass

Before declaring the plan complete:

1. `pytest tests/ -v` — all green against local Postgres.
2. `grep -rn itemdb timetagger/` — should return nothing under `timetagger/server/`. (`__main__.py` and the `--version` hook may still reference the module name in a print; remove or conditionalize.)
3. `grep -rn user2filename timetagger/` — only the definition in `_utils.py`; no callers.
4. `python -c "import api.index; import scripts.build_assets; import scripts.migrate; print('ok')"`.
5. Design doc's §14 "Open questions / risks" — check off or document anything hit during implementation.

Commit any cleanups as a final `chore: post-port cleanup` commit.
