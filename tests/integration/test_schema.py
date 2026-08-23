"""Schema creation against a real TimescaleDB: tables, hypertables, view."""

import asyncpg
import pytest

from .conftest import make_writer, table_exists


@pytest.mark.asyncio
async def test_creates_all_tables(writer, db):
    """A default configuration creates every table Scribe needs."""
    for name in (
        "entities",
        "states_raw",
        "events",
        "users",
        "areas",
        "devices",
        "integrations",
    ):
        assert await table_exists(db, name), f"{name} missing"
    # `states` is a view over states_raw, never a table.
    assert await table_exists(db, "states", kind="VIEW")
    assert not await table_exists(db, "states")


@pytest.mark.asyncio
async def test_states_raw_has_primary_key_and_index(writer, db):
    """The (metadata_id, time) PK must exist from creation, not from migration.

    Migration scripts rely on it for `ON CONFLICT (metadata_id, time)`, which
    fails outright if the constraint was never created.
    """
    async with db.acquire() as conn:
        pk_columns = await conn.fetch(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                               AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'states_raw'::regclass AND i.indisprimary
            ORDER BY a.attname
            """
        )
        assert [r["attname"] for r in pk_columns] == ["metadata_id", "time"]

        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_indexes "
            "WHERE tablename = 'states_raw' AND indexname = 'states_raw_meta_time_idx')"
        )


@pytest.mark.asyncio
async def test_entities_entity_id_is_unique(writer, db):
    """The UNIQUE constraint is what makes rename collisions detectable."""
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO entities (entity_id) VALUES ('sensor.dup')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO entities (entity_id) VALUES ('sensor.dup')")


@pytest.mark.asyncio
async def test_hypertables_are_created(writer, db):
    """states_raw and events are TimescaleDB hypertables, not plain tables."""
    async with db.acquire() as conn:
        names = await conn.fetch(
            "SELECT hypertable_name FROM timescaledb_information.hypertables"
        )
        got = {r["hypertable_name"] for r in names}
    assert {"states_raw", "events"} <= got


@pytest.mark.asyncio
async def test_states_view_joins_entity_ids(writer, db):
    """The compatibility view resolves metadata_id back to entity_id."""
    async with db.acquire() as conn:
        mid = await conn.fetchval(
            "INSERT INTO entities (entity_id) VALUES ('sensor.viewed') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO states_raw (time, metadata_id, state, value) "
            "VALUES (now(), $1, 'on', 1.0)",
            mid,
        )
        row = await conn.fetchrow(
            "SELECT entity_id, state, value FROM states WHERE entity_id = 'sensor.viewed'"
        )
    assert row["entity_id"] == "sensor.viewed"
    assert row["state"] == "on"
    assert row["value"] == 1.0


@pytest.mark.asyncio
async def test_init_is_idempotent(writer, db, hass):
    """Starting a second writer over an existing schema changes nothing."""
    async with db.acquire() as conn:
        before = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )

    second = make_writer(hass)
    await second.start()
    try:
        assert second._pool is not None
        async with second._pool.acquire() as conn:
            after = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        assert after == before
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_record_flags_skip_their_tables(hass, clean_db):
    """record_states=False / record_events=False leave those tables uncreated."""
    w = make_writer(hass, record_states=False, record_events=False)
    await w.start()
    try:
        assert not await table_exists(w._pool, "states_raw")
        assert not await table_exists(w._pool, "events")
        # Metadata tables are unaffected.
        assert await table_exists(w._pool, "entities")
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_optional_metadata_tables_can_be_disabled(hass, clean_db):
    """The enable_table_* flags each gate exactly one table."""
    w = make_writer(
        hass,
        enable_table_areas=False,
        enable_table_devices=False,
        enable_table_integrations=False,
        enable_table_users=False,
    )
    await w.start()
    try:
        for name in ("areas", "devices", "integrations", "users"):
            assert not await table_exists(w._pool, name), f"{name} should be absent"
        # entities is mandatory: every state write upserts into it.
        assert await table_exists(w._pool, "entities")
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_the_states_view_materializes_only_what_it_projects(writer, db):
    """The CTE feeding the lateral join must not carry the whole entities row.

    It once selected `*`, which materialized every entity's `capabilities`
    jsonb — usually the largest column in that table — on every query through
    the view, while only `id` and `entity_id` are ever used.
    """
    async with db.acquire() as conn:
        definition = await conn.fetchval(
            "SELECT pg_get_viewdef('states'::regclass, true)"
        )

    drive = definition.split("drive AS MATERIALIZED")[1].split(")")[0]
    assert "capabilities" not in drive
    assert "entity_id" in drive and "id" in drive
