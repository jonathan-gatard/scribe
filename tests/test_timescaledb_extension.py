"""`ensure_timescaledb` — the gate every new installation goes through.

It decides whether setup is refused, so each of its four outcomes matters:
already there, absent from the server, absent but installable, and absent with
no right to install it. It must never raise: a database that says no is a
`False`, which the caller turns into a refusal or a Repairs issue.
"""

from unittest.mock import AsyncMock

import pytest

from custom_components.scribe.writer import ensure_timescaledb


def _conn(installed=False, available=False, create_error=None, check_error=None):
    """A connection answering the three questions the function asks."""
    conn = AsyncMock()

    async def fetchval(sql, *args):
        if check_error:
            raise check_error
        if "pg_extension" in sql:
            return installed
        if "pg_available_extensions" in sql:
            return available
        return None

    async def execute(sql, *args):
        if create_error:
            raise create_error
        return "CREATE EXTENSION"

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(side_effect=execute)
    return conn


@pytest.mark.asyncio
async def test_an_installed_extension_is_left_alone():
    conn = _conn(installed=True)

    assert await ensure_timescaledb(conn) is True
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_forgotten_create_extension_is_run():
    """The most common cause of a missing extension is a skipped setup step."""
    conn = _conn(installed=False, available=True)

    assert await ensure_timescaledb(conn) is True
    assert "CREATE EXTENSION" in conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_an_extension_absent_from_the_server_is_not_attempted():
    """Nothing to enable on a PostgreSQL built without it — hosted ones mostly."""
    conn = _conn(installed=False, available=False)

    assert await ensure_timescaledb(conn) is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_right_to_create_the_extension_is_not_an_error():
    """A user without CREATE on the database is a refusal, not a crash."""
    conn = _conn(
        installed=False,
        available=True,
        create_error=Exception("permission denied to create extension"),
    )

    assert await ensure_timescaledb(conn) is False


@pytest.mark.asyncio
async def test_a_broken_connection_answers_no():
    """Called while connecting, so the connection can die under it."""
    conn = _conn(check_error=Exception("connection reset by peer"))

    assert await ensure_timescaledb(conn) is False


@pytest.mark.asyncio
async def test_an_unreadable_extension_catalogue_answers_no():
    """Some managed databases restrict pg_available_extensions."""
    conn = AsyncMock()

    async def fetchval(sql, *args):
        if "pg_extension" in sql:
            return False
        raise Exception("permission denied for table pg_available_extensions")

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock()

    assert await ensure_timescaledb(conn) is False
    conn.execute.assert_not_awaited()
