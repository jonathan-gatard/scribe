"""Failure and load behaviour against a real database.

A recorder's worst day is not a happy path with ten rows: it is the database
disappearing mid-flush, a restart with a full buffer, or a burst of thousands
of states from an integration that just came online.
"""

import asyncio
from datetime import timedelta

import pytest

from .conftest import BASE_TIME, entity_rows, make_writer, reconnect, write_states


def _state(entity_id, seconds):
    from datetime import timedelta

    return {
        "type": "state",
        "time": BASE_TIME + timedelta(seconds=seconds),
        "entity_id": entity_id,
        "state": "on",
        "value": float(seconds),
        "attributes": {},
    }


@pytest.mark.asyncio
async def test_buffered_states_are_written_once_the_database_returns(hass, clean_db):
    """A flush that fails must lose nothing: the next one writes everything."""
    w = make_writer(hass)
    await w.start()
    try:
        broken_pool = w._pool
        await broken_pool.close()

        for i in range(5):
            w._queue.append(_state("sensor.recovered", i))
        await w._flush()
        assert len(w._queue) == 5, "batch was not buffered"

        # Reconnect the way a restart would.
        await reconnect(w)
        await w._flush()

        _, count = await entity_rows(w._pool, "sensor.recovered")
        assert count == 5
        assert len(w._queue) == 0
    finally:
        if w._pool is not None:
            await w.stop()


@pytest.mark.asyncio
async def test_buffer_respects_max_queue_size(hass, clean_db):
    """Past the cap the oldest items are dropped, not the newest kept forever."""
    w = make_writer(hass, max_queue_size=10)
    await w.start()
    try:
        await w._pool.close()
        for i in range(25):
            w._queue.append(_state("sensor.capped", i))
        await w._flush()

        assert len(w._queue) <= 10
    finally:
        w._pool = None
        await w.stop()


@pytest.mark.asyncio
async def test_stop_flushes_what_is_pending(hass, clean_db):
    """Shutting down must not silently discard the queue."""
    w = make_writer(hass)
    await w.start()
    pool_dsn_writer = make_writer(hass)  # only used to read back afterwards
    for i in range(4):
        w._queue.append(_state("sensor.flushed_on_stop", i))

    await w.stop()

    await pool_dsn_writer.start()
    try:
        _, count = await entity_rows(pool_dsn_writer._pool, "sensor.flushed_on_stop")
        assert count == 4
    finally:
        await pool_dsn_writer.stop()


@pytest.mark.asyncio
async def test_large_batch_writes_in_one_flush(writer, db):
    """A burst of thousands of states is a single COPY, and loses none of them."""
    for i in range(5000):
        writer._queue.append(_state(f"sensor.bulk_{i % 50}", i))

    await writer._flush()

    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM states_raw") == 5000
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities WHERE entity_id LIKE 'sensor.bulk_%'"
            )
            == 50
        )


@pytest.mark.asyncio
async def test_many_new_entities_resolve_in_one_pass(writer, db):
    """First-sighting of hundreds of entities must not race or duplicate rows."""
    for i in range(300):
        writer._queue.append(_state(f"sensor.fresh_{i}", i))

    await writer._flush()

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities WHERE entity_id LIKE 'sensor.fresh_%'"
            )
            == 300
        )
        # No entity_id resolved to two different ids.
        assert (
            await conn.fetchval("SELECT count(DISTINCT metadata_id) FROM states_raw")
            == 300
        )


@pytest.mark.asyncio
async def test_concurrent_flushes_do_not_duplicate_or_lose_rows(writer, db):
    """Overlapping flushes are serialized by the metadata lock."""
    for i in range(200):
        writer._queue.append(_state("sensor.concurrent", i))

    await asyncio.gather(*(writer._flush() for _ in range(4)))

    _, count = await entity_rows(db, "sensor.concurrent")
    assert count == 200


@pytest.mark.asyncio
async def test_duplicate_timestamps_in_one_batch_do_not_kill_the_flush(writer, db):
    """Two states for one entity at the same instant violate the primary key.

    Home Assistant can emit them (a restored state and a live one sharing
    last_updated), and the whole batch must not be lost over it.
    """
    writer._queue.append(_state("sensor.dup_ts", 0))
    writer._queue.append(_state("sensor.dup_ts", 0))
    writer._queue.append(_state("sensor.dup_ts", 1))

    await writer._flush()

    _, count = await entity_rows(db, "sensor.dup_ts")
    assert count == 2, "the batch was lost to one duplicate timestamp"
    assert len(writer._queue) == 0


@pytest.mark.asyncio
async def test_batch_overlapping_written_history_still_lands(writer, db):
    """A re-buffered batch that overlaps rows already written must not stall.

    Without an ON CONFLICT fallback the COPY would fail on every retry, the
    queue would grow to its cap and Scribe would stop recording entirely.
    """
    for i in range(3):
        writer._queue.append(_state("sensor.replayed", i))
    await writer._flush()

    # Replay the same rows plus new ones, as a retry after a failure would.
    for i in range(5):
        writer._queue.append(_state("sensor.replayed", i))
    await writer._flush()

    _, count = await entity_rows(db, "sensor.replayed")
    assert count == 5
    assert len(writer._queue) == 0


@pytest.mark.asyncio
async def test_writer_survives_a_restart_with_existing_data(hass, clean_db):
    """A second run reuses the existing schema, ids and counts."""
    first = make_writer(hass)
    await first.start()
    await write_states(first, "sensor.persisted", 5)
    first_id, _ = await entity_rows(first._pool, "sensor.persisted")
    await first.stop()

    second = make_writer(hass)
    await second.start()
    try:
        await write_states(second, "sensor.persisted", 5, start=100)
        second_id, count = await entity_rows(second._pool, "sensor.persisted")
        assert second_id == first_id, "restart created a duplicate entity row"
        assert count == 10
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_server_side_error_still_buffers_the_batch(writer, db):
    """A PostgreSQL error is the case buffering exists for.

    Losing the connection raises a client-side error; a full disk, a revoked
    grant or a statement timeout raises a server-side `PostgresError`. Both
    mean "try again in a moment", and with `buffer_on_failure` enabled neither
    may cost a single state.
    """
    import asyncpg

    for i in range(5):
        writer._queue.append(
            {
                "type": "state",
                "time": BASE_TIME + timedelta(seconds=i),
                "entity_id": "sensor.kept",
                "state": f"s{i}",
                "value": float(i),
                "attributes": {},
            }
        )

    async def raise_server_error(*args, **kwargs):
        raise asyncpg.exceptions.DiskFullError("could not extend file: No space left")

    original = writer._copy_records
    writer._copy_records = raise_server_error
    try:
        await writer._flush()
    finally:
        writer._copy_records = original

    assert len(writer._queue) == 5, "the batch must be held for the next attempt"

    # And it lands once the database accepts writes again.
    await writer._flush()
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM states WHERE entity_id = 'sensor.kept'"
            )
            == 5
        )
