import os
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
