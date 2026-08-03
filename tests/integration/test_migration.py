"""End-to-end migration from the legacy `states` table to `states_raw`.

This is the riskiest path a real user hits: an existing installation upgrading
carries years of history in a table Scribe must rename, backfill and index
without losing a row. It only ever runs against a real database.
"""
from datetime import timedelta

import pytest

from custom_components.scribe import migration

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


@pytest.fixture
async def legacy_pool(clean_db):
    """A bare pool used to set up pre-migration state before any writer runs."""
    import asyncpg
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=2, max_inactive_connection_lifetime=0)
    yield pool
    await pool.close()


@pytest.mark.asyncio
async def test_legacy_states_table_is_renamed_on_start(hass, legacy_pool):
    """Startup renames `states` out of the way so states_raw can take the name."""
    await _create_legacy_states(legacy_pool, [
        (BASE_TIME, "sensor.old_one", "on", 1.0, None),
    ])

    w = make_writer(hass)
    await w.start()
    try:
        assert await table_exists(w._pool, "states_legacy")
        assert await table_exists(w._pool, "states_raw")
        # `states` is now the compatibility view, not the old table.
        assert await table_exists(w._pool, "states", kind="VIEW")
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_legacy_data_is_migrated_with_entities(hass, legacy_pool):
    """Every legacy row lands in states_raw under a resolved metadata_id."""
    rows = [
        (BASE_TIME + timedelta(seconds=i), f"sensor.legacy_{i % 3}",
         f"s{i}", float(i), None)
        for i in range(9)
    ]
    await _create_legacy_states(legacy_pool, rows)

    w = make_writer(hass)
    await w.start()
    try:
        await migration.migrate_states_data(w._pool)

        async with w._pool.acquire() as conn:
            # Three distinct entities were created from the legacy rows.
            assert await conn.fetchval(
                "SELECT count(*) FROM entities "
                "WHERE entity_id LIKE 'sensor.legacy_%'") == 3
            assert await conn.fetchval("SELECT count(*) FROM states_raw") == 9
            # And they resolve back through the view.
            assert await conn.fetchval(
                "SELECT count(*) FROM states "
                "WHERE entity_id LIKE 'sensor.legacy_%'") == 9
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_migration_is_idempotent(hass, legacy_pool):
    """Running the data migration twice must not duplicate history."""
    await _create_legacy_states(legacy_pool, [
        (BASE_TIME, "sensor.once", "on", 1.0, None),
        (BASE_TIME + timedelta(seconds=1), "sensor.once", "off", 0.0, None),
    ])

    w = make_writer(hass)
    await w.start()
    try:
        await migration.migrate_states_data(w._pool)
        await migration.migrate_states_data(w._pool)

        async with w._pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM states_raw") == 2
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_migration_without_legacy_table_is_a_noop(writer):
    """A fresh install has nothing to migrate and must report so."""
    assert await migration.migrate_states_data(writer._pool) is False


@pytest.mark.asyncio
async def test_empty_legacy_table_migrates_cleanly(hass, legacy_pool):
    """A legacy table with no rows must not crash on its NULL time range."""
    await _create_legacy_states(legacy_pool, [])

    w = make_writer(hass)
    await w.start()
    try:
        await migration.migrate_states_data(w._pool)
        async with w._pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM states_raw") == 0
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_constraint_migration_is_idempotent(writer):
    """states_raw already ships with its PK, so the migration step is a no-op."""
    result = await migration._migrate_states_raw_constraints(
        writer._pool, has_timescaledb=True)
    assert result is False


@pytest.mark.asyncio
async def test_timescaledb_is_detected(writer):
    """The extension check drives whether hypertable features are attempted."""
    assert await migration._check_timescaledb(writer._pool) is True


@pytest.mark.asyncio
async def test_states_raw_stays_a_hypertable_after_migration(hass, legacy_pool):
    """Migrated installs must end up with the same shape as fresh ones."""
    await _create_legacy_states(legacy_pool, [
        (BASE_TIME, "sensor.shape", "on", 1.0, None),
    ])

    w = make_writer(hass)
    await w.start()
    try:
        await migration.migrate_states_data(w._pool)
        await migration._convert_to_hypertable(w._pool)

        async with w._pool.acquire() as conn:
            names = {r["hypertable_name"] for r in await conn.fetch(
                "SELECT hypertable_name FROM timescaledb_information.hypertables")}
        assert "states_raw" in names
    finally:
        await w.stop()
