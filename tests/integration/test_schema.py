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
async def test_states_raw_has_its_primary_key(writer, db):
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
    assert "entity_id" in drive
    assert "id" in drive


@pytest.mark.asyncio
async def test_states_raw_carries_no_index_the_primary_key_duplicates(writer, db):
    """An index on (metadata_id, time DESC) serves nothing the key does not.

    A B-tree is scanned in either direction, so the primary key answers
    `WHERE metadata_id = x ORDER BY time DESC` on its own. Keeping both
    doubled the index footprint of the largest table and cost every write.
    """
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('states_raw_meta_time_idx') IS NULL"
        )


@pytest.mark.asyncio
async def test_an_index_left_by_an_earlier_version_is_dropped(hass, clean_db):
    """Existing installations carry it; starting up removes it."""
    import asyncpg

    from .conftest import DSN, make_writer

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TABLE states_raw (time TIMESTAMPTZ NOT NULL, "
            "metadata_id INTEGER NOT NULL, state TEXT, value DOUBLE PRECISION, "
            "attributes JSONB, PRIMARY KEY (metadata_id, time))"
        )
        await conn.execute(
            "CREATE INDEX states_raw_meta_time_idx ON states_raw (metadata_id, time DESC)"
        )
    finally:
        await conn.close()

    w = make_writer(hass)
    await w.start()
    try:
        async with w._pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT to_regclass('states_raw_meta_time_idx') IS NULL"
            )

            # And the query it used to serve still returns the right row.
            # Which index the planner picks depends on the data, so that is
            # left to the benchmark rather than asserted here.
            await conn.execute(
                "INSERT INTO states_raw (time, metadata_id, state, value, attributes) "
                "SELECT now() - (s || ' seconds')::interval, 1, 'on', s, NULL "
                "FROM generate_series(1, 2000) s"
            )
            newest = await conn.fetchval(
                "SELECT value FROM states_raw "
                "WHERE metadata_id = 1 ORDER BY time DESC LIMIT 1"
            )
        assert newest == 1.0
    finally:
        await w.stop()
