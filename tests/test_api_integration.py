import pytest
from timetagger.server._pgstore import AsyncPgDB
from timetagger.server._apiserver import _push_items, get_records


class FakeRequest:
    def __init__(self, method="GET", qs=None, body=None):
        self.method = method
        self.querydict = qs or {}
        self._body = body

    async def get_json(self, limit):
        return self._body


@pytest.mark.asyncio
async def test_put_and_get_records_roundtrip():
    db = AsyncPgDB("alice")
    await db.open()
    try:
        put_req = FakeRequest(
            method="PUT",
            body=[{"key": "r1", "mt": 1, "t1": 10, "t2": 20, "ds": "#work"}],
        )
        status, _, body = await _push_items(
            put_req, {"username": "alice"}, db, "records"
        )
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
    db = AsyncPgDB("alice")
    await db.open()
    try:
        put = FakeRequest(
            method="PUT",
            body=[
                {"key": "r1", "mt": 1, "t1": 10, "t2": 20, "ds": "#work done"},
                {"key": "r2", "mt": 1, "t1": 10, "t2": 20, "ds": "#home idle"},
            ],
        )
        await _push_items(put, {"username": "alice"}, db, "records")

        req = FakeRequest(qs={"timerange": "0-100", "tag": "work"})
        status, _, body = await get_records(req, {"username": "alice"}, db)
        assert status == 200
        keys = [r["key"] for r in body["records"]]
        assert keys == ["r1"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_users_are_isolated():
    a = AsyncPgDB("alice")
    await a.open()
    b = AsyncPgDB("bob")
    await b.open()
    try:
        await _push_items(
            FakeRequest(
                method="PUT",
                body=[{"key": "x", "mt": 1, "t1": 1, "t2": 2, "ds": "#a"}],
            ),
            {"username": "alice"},
            a,
            "records",
        )
        await _push_items(
            FakeRequest(
                method="PUT",
                body=[{"key": "x", "mt": 1, "t1": 1, "t2": 2, "ds": "#b"}],
            ),
            {"username": "bob"},
            b,
            "records",
        )
        _, _, body_a = await get_records(
            FakeRequest(qs={"timerange": "0-100"}),
            {"username": "alice"},
            a,
        )
        _, _, body_b = await get_records(
            FakeRequest(qs={"timerange": "0-100"}),
            {"username": "bob"},
            b,
        )
    finally:
        await a.close()
        await b.close()

    assert body_a["records"][0]["ds"] == "#a"
    assert body_b["records"][0]["ds"] == "#b"
