"""`chunk_time_interval` and `compress_after` against a real TimescaleDB.

Both settings used to apply only on the very first start: `create_hypertable`
and `add_compression_policy` are called with `if_not_exists => TRUE`, which
ignores its arguments once the object exists — so changing either value did
nothing while the log said it had. What these tests pin down is that the
database ends up matching the configuration, and that reaching that state costs
no data: chunks already written keep their span, chunks already compressed stay
compressed.
"""

from datetime import timedelta

import asyncpg
import pytest

from .conftest import BASE_TIME, DSN, make_writer, write_states


async def _chunk_interval(pool, table):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_name = $1 AND column_name = 'time'",
            table,
        )


async def _compression_job(pool, table):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT job_id, config ->> 'compress_after' AS compress_after "
            "FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_name = $1",
            table,
        )


async def _chunks(pool, table):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT chunk_name, range_end - range_start AS span, is_compressed "
            "FROM timescaledb_information.chunks "
            "WHERE hypertable_name = $1 ORDER BY range_start",
            table,
        )


@pytest.mark.asyncio
async def test_settings_are_applied_on_creation(hass, clean_db):
    w = make_writer(hass, chunk_interval="1 day", compress_after="60 days")
    await w.start()
    try:
        assert await _chunk_interval(w._pool, "states_raw") == timedelta(days=1)
        assert (await _compression_job(w._pool, "states_raw"))[
            "compress_after"
        ] == "60 days"
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_changing_them_actually_reaches_the_database(hass, clean_db):
    w = make_writer(hass, chunk_interval="1 day", compress_after="60 days")
    await w.start()
    await w.stop()

    w = make_writer(hass, chunk_interval="7 days", compress_after="10 days")
    await w.start()
    try:
        assert await _chunk_interval(w._pool, "states_raw") == timedelta(days=7)
        assert (await _compression_job(w._pool, "states_raw"))[
            "compress_after"
        ] == "10 days"
        # events carries the same two settings.
        assert await _chunk_interval(w._pool, "events") == timedelta(days=7)
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_unchanged_settings_change_nothing(hass, clean_db):
    """No churn on restart: re-creating the job would reset its schedule."""
    w = make_writer(hass, chunk_interval="1 day", compress_after="60 days")
    await w.start()
    before = await _compression_job(w._pool, "states_raw")
    await w.stop()

    w = make_writer(hass, chunk_interval="1 day", compress_after="60 days")
    await w.start()
    try:
        after = await _compression_job(w._pool, "states_raw")
    finally:
        await w.stop()

    assert before["job_id"] == after["job_id"]


@pytest.mark.asyncio
async def test_existing_chunks_keep_their_span(hass, clean_db):
    """Resizing is for future chunks: nothing already written is rewritten."""
    w = make_writer(hass, chunk_interval="1 day")
    await w.start()
    await write_states(w, "sensor.old_span", 3)
    rows_before = await _chunks(w._pool, "states_raw")
    await w.stop()

    w = make_writer(hass, chunk_interval="7 days")
    await w.start()
    try:
        # Write far enough ahead to land in a brand-new chunk.
        for i in range(3):
            w._queue.append(
                {
                    "type": "state",
                    "time": BASE_TIME + timedelta(days=30 + i),
                    "entity_id": "sensor.new_span",
                    "state": f"s{i}",
                    "value": float(i),
                    "attributes": {},
                }
            )
        await w._flush()

        spans = {
            r["chunk_name"]: r["span"] for r in await _chunks(w._pool, "states_raw")
        }
        for old in rows_before:
            assert spans[old["chunk_name"]] == timedelta(days=1)
        assert timedelta(days=7) in spans.values()

        async with w._pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states WHERE entity_id = 'sensor.old_span'"
                )
                == 3
            )
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_compressed_chunks_survive_a_policy_change(hass, clean_db):
    """Replacing the policy must not decompress anything."""
    w = make_writer(hass, chunk_interval="1 day", compress_after="60 days")
    await w.start()
    await write_states(w, "sensor.compressed", 5)
    async with w._pool.acquire() as conn:
        for row in await conn.fetch(
            "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'states_raw' AND NOT is_compressed"
        ):
            await conn.execute(
                f"SELECT compress_chunk('{row['chunk_schema']}.{row['chunk_name']}')"
            )
    compressed_before = [
        c["chunk_name"]
        for c in await _chunks(w._pool, "states_raw")
        if c["is_compressed"]
    ]
    assert compressed_before
    await w.stop()

    w = make_writer(hass, chunk_interval="1 day", compress_after="3 days")
    await w.start()
    try:
        after = {
            c["chunk_name"]: c["is_compressed"]
            for c in await _chunks(w._pool, "states_raw")
        }
        assert all(after[name] for name in compressed_before)
        assert (await _compression_job(w._pool, "states_raw"))[
            "compress_after"
        ] == "3 days"

        async with w._pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states WHERE entity_id = 'sensor.compressed'"
                )
                == 5
            )
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_plain_postgres_is_not_disturbed(hass, clean_db, monkeypatch):
    """Without TimescaleDB there is no dimension and no policy to sync."""
    w = make_writer(hass, chunk_interval="1 day")
    await w.start()
    try:
        # Nothing to assert beyond "it started and records": the sync helpers
        # must stay silent when the queries they rely on return nothing.
        await w._apply_chunk_interval("entities")  # a plain table, not a hypertable
        await w._apply_compression_policy("entities")
        await write_states(w, "sensor.plain", 2)
        async with w._pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states WHERE entity_id = 'sensor.plain'"
                )
                == 2
            )
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_a_hostile_chunk_interval_cannot_run_sql(hass, clean_db):
    """`chunk_time_interval` is free-form user input and reaches create_hypertable.

    Interpolated, this value closed the quoted interval and left a second
    statement behind, which asyncpg's simple query protocol runs like any
    other — verified against this database before the fix, canary and all.
    As a parameter it is only ever a malformed interval.
    """
    setup = await asyncpg.connect(DSN)
    try:
        await setup.execute("CREATE TABLE IF NOT EXISTS canary (x int)")
    finally:
        await setup.close()

    w = make_writer(
        hass, chunk_interval="7 days'); DROP TABLE canary; --", record_events=False
    )
    await w.start()
    try:
        async with w._pool.acquire() as conn:
            survived = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'canary')"
            )
    finally:
        await w.stop()
        cleanup = await asyncpg.connect(DSN)
        try:
            await cleanup.execute("DROP TABLE IF EXISTS canary")
        finally:
            await cleanup.close()

    assert survived, "the chunk interval executed SQL of its own"
