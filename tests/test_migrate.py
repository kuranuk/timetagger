import os
import asyncpg
import pytest
from scripts import migrate

DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.mark.asyncio
async def test_migrate_creates_tables():
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
async def test_migrate_is_idempotent():
    await migrate.run(DATABASE_URL)
    await migrate.run(DATABASE_URL)  # should not raise
