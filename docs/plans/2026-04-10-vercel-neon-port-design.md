# TimeTagger on Vercel + Neon — Design Doc

- Date: 2026-04-10
- Status: Approved (pending implementation plan)
- Target: self-hosted deployment on Vercel with Neon Postgres; single user primary,
  future 2–N users without code changes.

## 1. Goals

- Run the existing TimeTagger server on Vercel Functions (Fluid Compute, Python).
- Replace per-user SQLite (via `itemdb`) with a single Neon Postgres database.
- Keep the existing client (PScript → JS), API surface, and bcrypt credential
  auth untouched from the user's point of view.
- Minimize the diff in `timetagger/server/_apiserver.py`.
- Serve static assets (JS/CSS/HTML/images/service worker) from Vercel's Edge
  Network by pre-compiling them at build time; the Python Function serves the
  API only.

### Non-goals (YAGNI)

- Supabase Auth / Clerk / third-party IdP integration.
- Neon branching per preview deployment.
- Vercel Cron backups, Rolling Releases, Sign in with Vercel.
- PScript-to-Wasm rewrite.
- Content-hashed asset filenames (version suffix is good enough for now).

## 2. Why Neon over Supabase

TimeTagger's data layer is effectively a key-value store over JSON items with a
few numeric indexes (`_apiserver.py:61-72`). `itemdb` is a thin wrapper over
SQLite. Plain Postgres is a direct match.

Supabase bundles Auth, Realtime, Storage, Edge Functions. TimeTagger already has
its own bcrypt + JWT auth, and its client polls `/updates?since=...` rather than
using realtime push (`_apiserver.py:309`), so Supabase's differentiators add
only operational surface area without benefit.

Neon gives:

- Pure serverless Postgres with scale-to-zero and Vercel Marketplace native
  integration (auto-provisioned `DATABASE_URL`).
- Pooled endpoint (PgBouncer transaction-mode) suitable for Fluid Compute.
- Free tier that covers individual / few-user deployments comfortably.

## 3. Access control

Target deployment is single-user, but the design keeps multi-user open.

Decision: **keep TimeTagger's built-in bcrypt credential auth**
(`TIMETAGGER_CREDENTIALS`). `__main__.py:240` already accepts a comma- or
semicolon-separated list, so adding more users is an env-var change only.

Rejected alternatives:

- Basic auth via Vercel Routing Middleware — works but replaces the existing
  login UI with a browser dialog and duplicates logic.
- Vercel Deployment Protection / Password Protection — Pro-plan features, not
  cost-effective for personal use.
- Clerk / Auth0 / Supabase Auth — overkill for 1–N users on a self-host.

## 4. Data model (Neon / Postgres schema)

The three per-user itemdb tables (`records`, `settings`, `userinfo`) become
three shared tables partitioned by `username`. Fields previously queried via
`json_extract(_ob, '$.ds')` are promoted to real columns so queries translate
directly.

```sql
CREATE TABLE records (
  username TEXT             NOT NULL,
  key      TEXT             NOT NULL,
  st       DOUBLE PRECISION NOT NULL,
  mt       BIGINT           NOT NULL,
  t1       BIGINT           NOT NULL,
  t2       BIGINT           NOT NULL,
  ds       TEXT,
  ob       JSONB            NOT NULL,
  PRIMARY KEY (username, key)
);
CREATE INDEX records_st_idx ON records (username, st);
CREATE INDEX records_t1_idx ON records (username, t1);
CREATE INDEX records_t2_idx ON records (username, t2);

CREATE TABLE settings (
  username TEXT             NOT NULL,
  key      TEXT             NOT NULL,
  st       DOUBLE PRECISION NOT NULL,
  mt       BIGINT           NOT NULL,
  ob       JSONB            NOT NULL,
  PRIMARY KEY (username, key)
);
CREATE INDEX settings_st_idx ON settings (username, st);

CREATE TABLE userinfo (
  username TEXT             NOT NULL,
  key      TEXT             NOT NULL,
  st       DOUBLE PRECISION NOT NULL,
  mt       BIGINT           NOT NULL,
  ob       JSONB            NOT NULL,
  PRIMARY KEY (username, key)
);

-- Replaces itemdb's file-mtime-based `db.mtime` used by the /updates fast path
-- at _apiserver.py:328. Written on every mutation.
CREATE TABLE db_meta (
  username TEXT PRIMARY KEY,
  mtime    DOUBLE PRECISION NOT NULL
);
```

Rationale for promoting `t1`, `t2`, `ds` out of JSONB: the existing API in
`get_records` (`_apiserver.py:404-424`) filters on them directly. Real columns
make the query translation trivial and index-friendly.

## 5. Storage adapter

New module `timetagger/server/_pgstore.py` exposes `AsyncPgDB(username)` with
the same method shape as `itemdb.AsyncItemDB`:

- `open()` / `close()` — acquire/release a non-transactional connection from
  the pool (used by the API handler around a single request).
- `async with db:` — begin/commit (or rollback) a transaction, matching the
  existing `_apiserver.py:464` usage.
- `select(table, where, *params)` — translates `?` placeholders to asyncpg
  `$N`, always prepends `username = $1`.
- `select_one(table, where, *params)` — same, returns first row or `None`.
- `select_all(table)` — returns all rows for the user.
- `put(table, item)` / `put_one(table, **fields)` — upsert on
  `(username, key)`, also bumps `db_meta.mtime`.
- `mtime` — lazy-loaded from `db_meta` for the early-exit in `get_updates`.

Query translation notes:

- `itemdb` accepts SQLite-ish strings like `"key == 'reset_time'"`,
  `"st >= 1712345.0"`, `"key == ?"`. The adapter normalizes `==`→`=`, rewrites
  `?` placeholders to `$N`, and prepends the `username = $1` filter in the
  generated SQL. This is not a general SQL parser — it only supports the
  handful of shapes the existing code uses. If a new shape is needed, the
  adapter raises, forcing a conscious decision.
- `get_records`' compound query (`_apiserver.py:404-424`) is the one place that
  uses the ds LIKE form. The API code gets a one-line change from
  `json_extract(_ob, '$.ds') LIKE ?` to `ds LIKE ?`.
- Parameter placeholders in the API code move from `?` to `?` still — the
  adapter does the `$N` rewrite so the API layer stays portable.

Driver: `asyncpg` (fast, native async, no sync wrappers).

## 6. Connection pool

Fluid Compute reuses function instances across requests, so a module-global
pool is correct:

```python
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_dsn(),
            min_size=0,
            max_size=4,
            max_inactive_connection_lifetime=30,
            statement_cache_size=0,  # Neon pooled endpoint is PgBouncer txn mode
        )
    return _pool
```

Key settings:

- `min_size=0`: do not hold idle connections; lets Neon autosuspend.
- `max_size=4`: sized for per-instance concurrency on Fluid Compute, conservative
  for Neon connection limits.
- `statement_cache_size=0`: required when connecting through Neon's pooled
  endpoint (`-pooler` suffix), which uses PgBouncer in transaction mode and does
  not preserve prepared statements across transactions.

DSN resolution order:

1. `TIMETAGGER_DATABASE_URL` (explicit)
2. `DATABASE_URL` (Vercel Marketplace / Neon integration default)
3. `POSTGRES_URL`

## 7. Vercel Function entry

Ship the API only as a Python Function. Assets are pre-compiled to `public/`.

```
/
├── api/
│   └── index.py            # ASGI entry, API routes only
├── scripts/
│   ├── build_assets.py     # runs at vercel build; writes public/
│   └── migrate.py          # manual DB migrations
├── public/                 # build output (gitignored)
├── migrations/
│   └── 0001_init.sql
├── timetagger/             # existing package, minimal patches
├── vercel.ts
└── requirements.txt
```

`api/index.py`:

```python
import asgineer
from timetagger.server import authenticate, AuthException, api_handler_triage
from timetagger.__main__ import get_webtoken

@asgineer.to_asgi
async def app(request):
    path = request.path
    if path == "/api/v2/bootstrap_authentication":
        return await get_webtoken(request)
    if path.startswith("/api/v2/"):
        subpath = path.removeprefix("/api/v2/").strip("/")
        try:
            auth_info, db = await authenticate(request)
        except AuthException as err:
            return 401, {}, f"unauthorized: {err}"
        try:
            return await api_handler_triage(request, subpath, auth_info, db)
        finally:
            await db.close()
    return 404, {}, "not found"
```

`vercel.ts`:

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
      public: true, maxAge: '1 year', immutable: true,
    }),
  ],
};
```

## 8. Asset pre-compilation

`scripts/build_assets.py` reuses the existing asset helpers and writes them as
files under `public/`:

```python
import os
from importlib import resources
from timetagger.server import create_assets_from_dir, enable_service_worker

os.environ["TIMETAGGER_PATH_PREFIX"] = "/"

common  = create_assets_from_dir(resources.files("timetagger.common"))
apponly = create_assets_from_dir(resources.files("timetagger.app"))
image   = create_assets_from_dir(resources.files("timetagger.images"))
page    = create_assets_from_dir(resources.files("timetagger.pages"))

app_assets = {**common, **image, **apponly}
web_assets = {**common, **image, **page}
enable_service_worker(app_assets)

write_assets("public/app", app_assets)
write_assets("public",     web_assets)
```

Notes:

- Asset dicts hold mixed `str`/`bytes`; `write_assets` dispatches on type.
- PScript compile, SCSS compile, Markdown → HTML, Jinja templating all run in
  the Vercel build container; none require network or DB access.
- `enable_service_worker` embeds `path_prefix`, so `TIMETAGGER_PATH_PREFIX=/`
  must be set before importing the helper.
- Build container needs `requirements.txt` deps installed (Vercel does this
  automatically for Python runtime).

## 9. Migrations

Lightweight hand-rolled runner, not Alembic.

- `migrations/0001_init.sql` contains the CREATE TABLE statements from §4.
- `scripts/migrate.py` reads `migrations/*.sql` in lexicographic order, applies
  any not recorded in a `schema_migrations (version TEXT PRIMARY KEY, applied_at
  TIMESTAMPTZ)` table, and records each on success.
- Run manually after schema changes with `DATABASE_URL=... python
  scripts/migrate.py`. Not triggered by `vercel build`.

## 10. Patches to existing files

### `timetagger/server/_utils.py:55-73` — JWT key

The current implementation writes a generated key to
`~/.timetagger/jwt.key`. On Vercel's ephemeral filesystem this means every cold
start generates a new key and invalidates all live JWTs.

Fix: prefer env var, fall back to file for local dev.

```python
def _load_jwt_key():
    secret = os.environ.get("TIMETAGGER_JWT_SECRET", "").strip()
    if secret:
        return secret
    filename = os.path.join(ROOT_TT_DIR, "jwt.key")
    # ...existing file-based logic unchanged
```

### `timetagger/server/_apiserver.py:10, 188, 287`

Replace the itemdb import and the two `AsyncItemDB(dbname)` call sites with
`AsyncPgDB(username)` + `await db.open()`. Remove the three `ensure_table`
calls — schema is managed by migrations now.

### `timetagger/server/_apiserver.py:404-424` — `get_records`

Rewrite tag/hidden LIKE clauses to use the new `ds` column:

```python
query_parts.append("ds LIKE ? ESCAPE '\\' OR ds LIKE ? ESCAPE '\\'")
# ...
query_parts.append("ds LIKE 'HIDDEN%'")  # and NOT LIKE variant
```

### `timetagger/server/_utils.py:31` — `user2filename`

Leave in place but unused. No breakage for external importers.

## 11. Environment variables

| Variable | Purpose | Example |
|---|---|---|
| `TIMETAGGER_DATABASE_URL` (or `DATABASE_URL`) | Neon pooled endpoint | `postgres://user:pass@ep-xxx-pooler.neon.tech/neondb?sslmode=require` |
| `TIMETAGGER_CREDENTIALS` | bcrypt hashes, comma-separated | `alice:$2a$08$...,bob:$2a$08$...` |
| `TIMETAGGER_JWT_SECRET` | HS256 signing key | 32 random bytes (base64) |
| `TIMETAGGER_PATH_PREFIX` | Must be `/` on Vercel | `/` |

Provision via Neon's Vercel Marketplace integration for `DATABASE_URL`; set the
others with `vercel env add`.

## 12. Testing

1. **Unit tests for `_pgstore.py`** — CRUD, transactions, `mtime` tracking,
   two-user isolation. Run against a local Postgres or GitHub Actions
   `services: postgres:16`.
2. **API integration tests** — run the existing `tests/` suite against
   `_pgstore.AsyncPgDB`. A `conftest.py` fixture reads `DATABASE_URL` from env
   and resets the schema between tests via `DROP SCHEMA public CASCADE; CREATE
   SCHEMA public;` followed by `scripts/migrate.py`.
3. **Two-user isolation test** — write data as alice and bob, assert neither
   sees the other's records/settings/userinfo.
4. **Delete the old SQLite-based test fixtures**. Do not maintain two
   backends.

CI: add `postgres:16` as a service in the GitHub Actions workflow, export
`DATABASE_URL=postgres://postgres:postgres@localhost:5432/postgres`.

## 13. Deployment checklist

1. Provision Neon via Vercel Marketplace → `DATABASE_URL` auto-injected.
2. `vercel env add TIMETAGGER_CREDENTIALS` (bcrypt hashes).
3. `vercel env add TIMETAGGER_JWT_SECRET` (random 32 bytes).
4. `vercel env add TIMETAGGER_PATH_PREFIX /`.
5. Locally: `DATABASE_URL=... python scripts/migrate.py` to create tables.
6. `vercel deploy --prod`.
7. Visit `/login`, sign in with a configured username/password.

## 14. Open questions / risks

- **asyncpg + Neon pooled endpoint edge cases**: if we hit prepared-statement
  issues beyond `statement_cache_size=0`, fall back to the direct endpoint and
  accept colder starts.
- **PScript compile time in the build container**: if it exceeds a reasonable
  budget, switch to caching the compiled output in a `build-cache/` directory
  committed to the repo.
- **Build container Python version**: Vercel defaults to Python 3.13; verify
  `pscript`, `asgineer`, `itemdb` (removed), `bcrypt`, `pyjwt` all install
  cleanly. If not, pin Python version in `vercel.ts`.
