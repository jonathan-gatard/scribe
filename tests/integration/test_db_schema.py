"""Configurable schema, against a real TimescaleDB (issue #53).

The mocked suite asserts on the SQL Scribe issues; these assert on where the
rows, hypertables and policies actually landed — which is the whole point of
the setting, and the one thing a mock cannot show.
"""

from datetime import timedelta

import asyncpg
import pytest

from custom_components.scribe.writer import ScribeWriter, WriterConfig

from .conftest import BASE_TIME, DSN, dsn_reachable, make_writer

SCHEMA = "scribe_alt"
ROLE = "scribe_no_create"


def _state(entity_id, value, seconds=0):
    """One queued state, in the shape the writer's flush path expects."""
    return {
        "type": "state",
        "time": BASE_TIME + timedelta(seconds=seconds),
        "entity_id": entity_id,
        "state": value,
        "value": None,
        "attributes": {},
    }


@pytest.fixture
async def clean_schema(socket_enabled):
    """A database with no `scribe_alt` schema, before and after the test."""
    if not await dsn_reachable():
        pytest.skip(f"no TimescaleDB at {DSN}")

    async def drop():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        finally:
            await conn.close()

    await drop()
    yield
    await drop()


@pytest.fixture
async def schema_writer(hass, clean_db, clean_schema):
    """A started writer recording into `scribe_alt` rather than `public`."""
    w = make_writer(hass, db_schema=SCHEMA)
    await w.start()
    assert w._pool is not None, "writer failed to connect"
    yield w
    await w.stop()


async def _relations_in(conn, schema):
    rows = await conn.fetch(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relkind IN ('r', 'v')
        """,
        schema,
    )
    return {r["relname"] for r in rows}


@pytest.mark.asyncio
async def test_every_table_is_created_in_the_configured_schema(schema_writer):
    async with schema_writer._pool.acquire() as conn:
        created = await _relations_in(conn, SCHEMA)

    assert {"states_raw", "states", "events", "entities"} <= created


@pytest.mark.asyncio
async def test_public_is_left_untouched(schema_writer):
    """The setting is only worth having if it moves *everything*."""
    async with schema_writer._pool.acquire() as conn:
        in_public = await _relations_in(conn, "public")

    assert not {"states_raw", "states", "events", "entities"} & in_public


@pytest.mark.asyncio
async def test_states_are_written_into_the_configured_schema(schema_writer):
    schema_writer.enqueue(_state("sensor.moved", "21.5"))
    await schema_writer._flush()

    async with schema_writer._pool.acquire() as conn:
        rows = await conn.fetchval(
            f'SELECT count(*) FROM "{SCHEMA}".states_raw'  # noqa: S608 - test constant
        )
    assert rows == 1


@pytest.mark.asyncio
async def test_the_view_reads_the_history_back(schema_writer):
    """`SELECT * FROM states` must work unqualified, as every doc example does."""
    schema_writer.enqueue(_state("sensor.moved", "21.5"))
    await schema_writer._flush()

    rows = await schema_writer.query("SELECT entity_id, state FROM states")

    assert [dict(r) for r in rows] == [{"entity_id": "sensor.moved", "state": "21.5"}]


@pytest.mark.asyncio
async def test_the_hypertable_belongs_to_the_configured_schema(schema_writer):
    async with schema_writer._pool.acquire() as conn:
        schemas = await conn.fetch(
            "SELECT hypertable_schema FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'states_raw'"
        )

    assert [r["hypertable_schema"] for r in schemas] == [SCHEMA]


@pytest.mark.asyncio
async def test_retention_applies_to_this_schemas_hypertable(
    hass, clean_db, clean_schema
):
    """A policy is per-hypertable, so it must follow the table into the schema."""
    writer = make_writer(hass, db_schema=SCHEMA, retention_states="30 days")
    await writer.start()
    try:
        async with writer._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT hypertable_schema, config ->> 'drop_after' AS drop_after
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_retention'
                  AND hypertable_name = 'states_raw'
                """
            )
    finally:
        await writer.stop()

    assert row["hypertable_schema"] == SCHEMA
    assert row["drop_after"] == "30 days"


@pytest.mark.asyncio
async def test_two_schemas_keep_separate_histories(hass, clean_db, clean_schema):
    """The use case from #53: one database, two independent Scribe tables."""
    public_writer = make_writer(hass)
    alt_writer = make_writer(hass, db_schema=SCHEMA)
    await public_writer.start()
    await alt_writer.start()

    try:
        for w, count in ((public_writer, 1), (alt_writer, 3)):
            for i in range(count):
                w.enqueue(_state("sensor.split", str(i), seconds=i))
            await w._flush()

        async with public_writer._pool.acquire() as conn:
            in_public = await conn.fetchval("SELECT count(*) FROM public.states_raw")
            in_alt = await conn.fetchval(
                f'SELECT count(*) FROM "{SCHEMA}".states_raw'  # noqa: S608
            )
    finally:
        await public_writer.stop()
        await alt_writer.stop()

    assert (in_public, in_alt) == (1, 3)


@pytest.mark.asyncio
async def test_statistics_count_only_this_schemas_chunks(hass, clean_db, clean_schema):
    """Same table name in two schemas must not have its chunks counted twice."""
    public_writer = make_writer(hass)
    alt_writer = make_writer(hass, db_schema=SCHEMA)
    await public_writer.start()
    await alt_writer.start()
    try:
        stats = await alt_writer.get_db_stats("chunk")
        async with alt_writer._pool.acquire() as conn:
            chunks_here = await conn.fetchval(
                "SELECT count(*) FROM timescaledb_information.chunks "
                "WHERE hypertable_name = 'states_raw' AND hypertable_schema = $1",
                SCHEMA,
            )
    finally:
        await public_writer.stop()
        await alt_writer.stop()

    assert stats["states_total_chunks"] == chunks_here


@pytest.mark.asyncio
async def test_a_legacy_table_in_public_does_not_block_another_schema(
    hass, clean_db, clean_schema
):
    """Legacy detection is by relation name: it has to be schema-scoped."""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TABLE public.states (time TIMESTAMPTZ, entity_id TEXT)"
        )
    finally:
        await conn.close()

    writer = make_writer(hass, db_schema=SCHEMA)
    await writer.start()
    try:
        assert writer._legacy_blocked is False
        assert writer._connected is True
    finally:
        await writer.stop()


async def _drop_test_role(conn):
    """Remove the role and the privilege entries that depend on it.

    A GRANT (or an explicit REVOKE) on the database is a dependency in its own
    right: `DROP ROLE` fails with DependentObjectsStillExist until it is gone.
    """
    if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
        await conn.execute(f"DROP OWNED BY {ROLE}")
        await conn.execute(f"DROP ROLE {ROLE}")


@pytest.mark.asyncio
async def test_a_schema_that_cannot_be_reached_records_nothing(hass, clean_db):
    """A role with no rights must not silently fill `public` instead."""
    conn = await asyncpg.connect(DSN)
    try:
        await _drop_test_role(conn)
        await conn.execute(
            "CREATE ROLE scribe_no_create LOGIN PASSWORD 'nope' NOCREATEDB"
        )
        await conn.execute(
            "REVOKE CREATE ON DATABASE scribe FROM scribe_no_create, PUBLIC"
        )
        await conn.execute("GRANT CONNECT ON DATABASE scribe TO scribe_no_create")
    finally:
        await conn.close()

    dsn = DSN.split("://")[0] + "://scribe_no_create:nope@" + DSN.split("@")[1]
    writer = ScribeWriter(
        hass,
        WriterConfig(db_url=dsn, db_schema="forbidden_schema", flush_interval=3600),
    )
    try:
        await writer.start()
        assert writer._schema_blocked is True
        assert writer._connected is False

        writer._running = True
        writer.enqueue({"type": "state", "entity_id": "sensor.x"})
        assert len(writer._queue) == 0
    finally:
        await writer.stop()
        conn = await asyncpg.connect(DSN)
        try:
            await _drop_test_role(conn)
            await conn.execute("GRANT CREATE ON DATABASE scribe TO PUBLIC")
        finally:
            await conn.close()
