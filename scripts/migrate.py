"""Apply SQL files in supabase/migrations/ in order, track in schema_migrations.

Reads from the Supabase CLI convention directory so the same files work with
`supabase db push` (primary, CLI-driven) and this Python runner (fallback,
used by CI and ad-hoc remote runs). Lexicographic sort matches Supabase's
`YYYYMMDDHHMMSS_<name>.sql` naming.

Prefers POSTGRES_URL_NON_POOLING (port 5432, direct) over the pooled endpoint,
because Supavisor transaction-mode does not support all DDL / advisory locks
we rely on during migration.
"""
import asyncio
import os
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
