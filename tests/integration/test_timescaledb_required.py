"""Scribe requires TimescaleDB — enabled automatically, or setup refused.

Scribe is a TimescaleDB integration: on plain PostgreSQL it still records, but
chunking, compression, retention and every size sensor do nothing. The rule
these tests pin down is that a *new* installation cannot end up there by
accident, while an existing one is never cut off.
"""

import asyncpg
import pytest

from custom_components.scribe.config_flow import _check_database
from custom_components.scribe.writer import ensure_timescaledb

from .conftest import DSN, make_writer, write_states


async def _drop_extension(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP EXTENSION IF EXISTS timescaledb CASCADE")
        return await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'timescaledb')"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_extension_is_enabled_when_missing(clean_db, socket_enabled):
    """A forgotten `CREATE EXTENSION` is fixed rather than reported."""
    assert await _drop_extension(DSN) is False

    conn = await asyncpg.connect(DSN)
    try:
        assert await ensure_timescaledb(conn) is True
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'timescaledb')"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_already_enabled_is_a_no_op(clean_db, socket_enabled):
    conn = await asyncpg.connect(DSN)
    try:
        assert await ensure_timescaledb(conn) is True
        assert await ensure_timescaledb(conn) is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_setup_is_accepted_on_a_timescaledb_database(clean_db, socket_enabled):
    assert await _check_database(DSN) is None


@pytest.mark.asyncio
async def test_setup_is_refused_when_it_cannot_be_enabled(
    clean_db, socket_enabled, monkeypatch
):
    """Without the extension and without the rights to add it, setup stops."""

    async def cannot(conn):
        return False

    monkeypatch.setattr(
        "custom_components.scribe.config_flow.ensure_timescaledb", cannot
    )

    assert await _check_database(DSN) == "no_timescaledb"


@pytest.mark.asyncio
async def test_unreachable_database_is_a_connection_error(socket_enabled):
    """The two refusals must stay distinguishable: they need different fixes."""
    assert (
        await _check_database("postgresql://postgres:scribe@127.0.0.1:1/scribe")
        == "cannot_connect"
    )


@pytest.mark.asyncio
async def test_a_started_writer_enables_it_too(hass, clean_db, socket_enabled):
    """The gate is in the config flow; the writer must not depend on it."""
    assert await _drop_extension(DSN) is False

    w = make_writer(hass)
    await w.start()
    try:
        assert w._has_timescaledb is True
        await write_states(w, "sensor.after_enable", 3)
        async with w._pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'states_raw')"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states "
                    "WHERE entity_id = 'sensor.after_enable'"
                )
                == 3
            )
    finally:
        await w.stop()
