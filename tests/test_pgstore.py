import os
import asyncpg
import pytest
from scripts import migrate
from timetagger.server._pgstore import AsyncPgDB, get_pool, reset_pool

DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
async def fresh_schema():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    await reset_pool()
    os.environ["TIMETAGGER_DATABASE_URL"] = DATABASE_URL
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
