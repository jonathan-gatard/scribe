"""A pre-3.0 database, against a real PostgreSQL.

Converting the old schema was dropped in 3.9; Scribe now stops and points at
the last version that can do it. The property that matters here is that
stopping is *inert* — an aborted start must leave the old database byte for
byte as it was, so installing 3.8 still converts it.
"""

import pytest

from homeassistant.helpers import issue_registry as ir

from .conftest import BASE_TIME, DSN, make_writer, table_exists


async def _create_legacy_states(pool, rows):
    """Build the pre-3.x `states` table and fill it with (entity_id, time) rows."""
    async with pool.acquire() as conn:
        await conn.execute("DROP VIEW IF EXISTS states CASCADE")
        await conn.execute("""
            CREATE TABLE states (
                time TIMESTAMPTZ NOT NULL,
                entity_id TEXT NOT NULL,
                state TEXT,
                value DOUBLE PRECISION,
                attributes JSONB
            )
        """)
        await conn.executemany(
            "INSERT INTO states (time, entity_id, state, value, attributes) "
            "VALUES ($1, $2, $3, $4, $5)",
            rows,
        )


async def _create_legacy_entities(pool):
    """The pre-3.0 `entities` table: keyed by entity_id text, no SERIAL id."""
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS entities CASCADE")
        await conn.execute("""
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                unique_id TEXT,
                platform TEXT
            )
        """)


@pytest.fixture
async def legacy_pool(clean_db):
    """A bare pool used to set up pre-migration state before any writer runs."""
    import asyncpg

    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, max_inactive_connection_lifetime=0
    )
    yield pool
    await pool.close()


@pytest.mark.asyncio
async def test_legacy_states_table_stops_the_writer(hass, legacy_pool):
    w = make_writer(hass)
    await _create_legacy_states(
        legacy_pool, [(BASE_TIME, "sensor.old_one", "on", 1.0, None)]
    )

    await w.start()
    try:
        assert w._legacy_blocked is True
        assert w._connected is False
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_legacy_database_is_left_exactly_as_it_was(hass, legacy_pool):
    """No rename, no new table, no dropped row: 3.8 must still find its input."""
    await _create_legacy_states(
        legacy_pool,
        [(BASE_TIME, "sensor.keep_me", "on", 1.0, None)],
    )

    w = make_writer(hass)
    await w.start()
    try:
        assert await table_exists(w._pool, "states", kind="BASE TABLE")
        assert not await table_exists(w._pool, "states_legacy")
        assert not await table_exists(w._pool, "states_raw")

        async with w._pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM states") == 1
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_legacy_entities_table_is_detected(hass, legacy_pool):
    """entities without a SERIAL id would make every write fail row by row."""
    await _create_legacy_entities(legacy_pool)

    w = make_writer(hass)
    await w.start()
    try:
        assert w._legacy_blocked is True
        async with w._pool.acquire() as conn:
            # The old table still has its text primary key and no id column.
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.columns "
                "WHERE table_name = 'entities' AND column_name = 'id')"
            )
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_interrupted_migration_is_detected(hass, legacy_pool):
    """A states_legacy left behind by an older Scribe still holds the history."""
    async with legacy_pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE states_legacy (time TIMESTAMPTZ NOT NULL, entity_id TEXT)"
        )

    w = make_writer(hass)
    await w.start()
    try:
        assert w._legacy_blocked is True
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_repairs_issue_names_the_version_to_install(hass, legacy_pool):
    await _create_legacy_states(
        legacy_pool, [(BASE_TIME, "sensor.old_one", "on", 1.0, None)]
    )

    w = make_writer(hass)
    await w.start()
    try:
        issue = ir.async_get(hass).async_get_issue("scribe", "legacy_schema")
        assert issue is not None
        assert issue.translation_placeholders == {
            "relation": "states",
            "version": "3.8",
        }
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_a_fresh_database_is_never_mistaken_for_legacy(writer, db):
    """The gate must be invisible on every install that is not pre-3.0."""
    assert writer._legacy_blocked is False
    assert writer._connected is True
    assert await table_exists(db, "states_raw")
    assert await table_exists(db, "states", kind="VIEW")

    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.columns "
            "WHERE table_name = 'entities' AND column_name = 'id')"
        )


@pytest.mark.asyncio
async def test_restart_after_conversion_recovers(hass, legacy_pool):
    """Once the legacy relations are gone, the next start is a normal one."""
    await _create_legacy_states(
        legacy_pool, [(BASE_TIME, "sensor.old_one", "on", 1.0, None)]
    )

    w = make_writer(hass)
    await w.start()
    assert w._legacy_blocked is True
    await w.stop()

    # Stand in for what 3.8 (or a manual cleanup) leaves behind.
    async with legacy_pool.acquire() as conn:
        await conn.execute("DROP TABLE states CASCADE")

    w = make_writer(hass)
    await w.start()
    try:
        assert w._legacy_blocked is False
        assert await table_exists(w._pool, "states_raw")
        assert await table_exists(w._pool, "states", kind="VIEW")
        # And the Repairs issue retires itself rather than lingering.
        assert ir.async_get(hass).async_get_issue("scribe", "legacy_schema") is None
    finally:
        await w.stop()
