"""End-to-end stats and the read-only query service, against real data."""

import asyncpg
import pytest

from .conftest import make_writer, write_event, write_states


@pytest.mark.asyncio
async def test_stats_report_real_chunks(writer, db):
    """Chunk counts come from TimescaleDB and reflect actual written data."""
    await write_states(writer, "sensor.stats", 5)
    await write_event(writer, "stats_event")

    stats = await writer.get_db_stats("all")

    assert stats["states_total_chunks"] >= 1
    assert stats["states_uncompressed_chunks"] >= 1
    assert stats["states_compressed_chunks"] == 0  # nothing old enough yet
    assert stats["events_total_chunks"] >= 1
    # Sizes are real byte counts, not placeholders.
    assert stats["states_total_size"] > 0
    assert stats["events_total_size"] > 0


@pytest.mark.asyncio
async def test_stats_types_are_selective(writer, db):
    """stats_type narrows the work done: 'chunk' must not compute sizes."""
    await write_states(writer, "sensor.stats", 1)

    chunk_only = await writer.get_db_stats("chunk")
    size_only = await writer.get_db_stats("size")

    assert "states_total_chunks" in chunk_only
    assert "states_total_size" not in chunk_only
    assert "states_total_size" in size_only
    assert "states_total_chunks" not in size_only


@pytest.mark.asyncio
async def test_stats_skip_disabled_tables(hass, clean_db):
    """With record_events=False no events stats are produced."""
    w = make_writer(hass, record_events=False)
    await w.start()
    try:
        await write_states(w, "sensor.only_states", 1)
        stats = await w.get_db_stats("all")
        assert "states_total_chunks" in stats
        assert not [k for k in stats if k.startswith("events_")]
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_stats_empty_without_a_pool(hass, clean_db):
    """A disconnected writer reports no stats rather than raising."""
    w = make_writer(hass)
    assert await w.get_db_stats("all") == {}


@pytest.mark.asyncio
async def test_query_returns_rows_as_dicts(writer, db):
    """The query service hands back plain dicts the service layer can serialize."""
    await write_states(writer, "sensor.queried", 3)

    rows = await writer.query(
        "SELECT entity_id, state, value FROM states "
        "WHERE entity_id = 'sensor.queried' ORDER BY time"
    )

    assert len(rows) == 3
    assert isinstance(rows[0], dict)
    assert rows[0]["entity_id"] == "sensor.queried"
    assert [r["state"] for r in rows] == ["s0", "s1", "s2"]


@pytest.mark.asyncio
async def test_query_is_read_only(writer, db):
    """Writes through the query service are rejected by the transaction itself."""
    await write_states(writer, "sensor.readonly", 1)

    with pytest.raises(asyncpg.PostgresError) as excinfo:
        await writer.query("DELETE FROM states_raw")
    assert "read-only" in str(excinfo.value).lower()

    # The data is still there.
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM states_raw") == 1


@pytest.mark.asyncio
async def test_query_rejects_ddl_too(writer, db):
    """Schema changes are covered by the same read-only guard."""
    with pytest.raises(asyncpg.PostgresError):
        await writer.query("DROP TABLE entities CASCADE")

    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'entities')"
        )


@pytest.mark.asyncio
async def test_query_propagates_sql_errors(writer, db):
    """A bad query raises rather than silently returning nothing."""
    with pytest.raises(asyncpg.UndefinedTableError):
        await writer.query("SELECT * FROM table_that_does_not_exist")


@pytest.mark.asyncio
async def test_query_without_a_pool_raises(hass, clean_db):
    """Querying a disconnected writer is an error, not an empty result."""
    w = make_writer(hass)
    with pytest.raises(RuntimeError, match="not connected"):
        await w.query("SELECT 1")


@pytest.mark.asyncio
async def test_initial_counts_come_from_the_database(hass, clean_db):
    """A restarting writer picks up the row counts already in the tables.

    Only when the I/O statistics sensors are enabled: seeding these counters
    aggregates the whole history, so an install that does not display them
    should not pay for it at every start.
    """
    first = make_writer(hass, enable_stats_io=True)
    await first.start()
    try:
        await write_states(first, "sensor.persisted", 4)
        await write_event(first, "persisted_event")
    finally:
        await first.stop()

    second = make_writer(hass, enable_stats_io=True)
    await second.start()
    try:
        await second._get_initial_counts()
        assert second._states_written == 4
        assert second._events_written == 1
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_query_results_can_be_serialized_by_home_assistant(writer, db):
    """`scribe.query` answers a service call, so its rows have to be JSON.

    A query selecting a `numeric` — EXTRACT(EPOCH …), avg(), any ::numeric —
    or an `interval` used to come back as Decimal and timedelta, which Home
    Assistant cannot serialize: the caller got an obscure error instead of
    rows.
    """
    import json

    from homeassistant.helpers.json import JSONEncoder

    rows = await writer.query(
        "SELECT EXTRACT(EPOCH FROM now()) AS epoch, "
        "1.5::numeric AS exact, "
        "INTERVAL '7 days' AS span, "
        "now() AS moment"
    )

    json.dumps(rows, cls=JSONEncoder)  # the bar: this must not raise

    assert isinstance(rows[0]["epoch"], float)
    assert rows[0]["exact"] == 1.5
    assert rows[0]["span"] == 604800.0


@pytest.mark.asyncio
async def test_the_initial_count_reads_the_table_not_the_view(hass, clean_db):
    """Both give the same number; only one of them is cheap.

    The `states` view reaches those rows through a lateral join on entities —
    measured at four times the cost of counting the hypertable directly, paid
    at every start by anyone with the I/O sensors enabled.
    """
    from .conftest import make_writer, write_states

    w = make_writer(hass, enable_stats_io=True)
    await w.start()
    try:
        await write_states(w, "sensor.counted", 5)
    finally:
        await w.stop()

    seen = []
    w2 = make_writer(hass, enable_stats_io=True)
    await w2.start()
    try:
        original = w2._row_count

        async def record(relation):
            seen.append(relation)
            return await original(relation)

        w2._row_count = record
        await w2._get_initial_counts()

        assert "states_raw" in seen
        assert "states" not in seen
        assert w2._states_written == 5
    finally:
        await w2.stop()
