import os
import pytest
from timetagger.server._pgstore import AsyncPgDB

DATABASE_URL = os.environ["DATABASE_URL"]


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
