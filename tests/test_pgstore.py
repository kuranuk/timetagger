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


@pytest.mark.asyncio
async def test_put_one_then_select_one_userinfo():
    db = AsyncPgDB("alice")
    await db.open()
    try:
        async with db:
            await db.put_one("userinfo", key="reset_time", st=1.0, mt=1, value=42.0)
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
