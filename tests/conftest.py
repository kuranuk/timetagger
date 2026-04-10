import os
import asyncpg
import pytest
from scripts import migrate
from timetagger.server._pgstore import reset_pool

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
