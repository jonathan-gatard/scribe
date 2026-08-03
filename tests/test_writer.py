"""Test ScribeWriter."""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from collections import deque
from custom_components.scribe.writer import ScribeWriter


@pytest.fixture
async def writer(hass, mock_pool):
    """Create a writer instance."""
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=2,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert="/tmp/root.crt",
        ssl_cert_file="/tmp/client.crt",
        ssl_key_file="/tmp/client.key",
    )
    writer._pool = mock_pool

    yield writer
    if writer._task:
        await writer.stop()


@pytest.mark.asyncio
async def test_writer_init_db(writer, mock_pool, mock_db_connection):
    """Test database initialization."""
    with patch("custom_components.scribe.migration.migrate_database") as mock_migrate:
        # Mock fetchval to return False for the table check so view is created
        async def fetchval_side_effect(sql, *args):
            if "information_schema.tables" in sql:
                return False
            return 0

        mock_db_connection.fetchval.side_effect = fetchval_side_effect

        await writer.start()
        await asyncio.sleep(0.1)  # Let background init finish

        # Fire HA started event to trigger migration
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        writer.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await writer.hass.async_block_till_done()
        await asyncio.sleep(0.1)  # Let migration task get scheduled and run

        # Verify migration was scheduled
        mock_migrate.assert_called_once()

    # Verify engine creation
    assert writer._pool == mock_pool

    # Verify table creation calls
    calls = []
    for call in mock_db_connection.execute.mock_calls:
        if call.args:
            sql = call.args[0]
            if not isinstance(sql, str) and hasattr(sql, "text"):
                sql = sql.text
            calls.append(str(sql))

    for call in mock_db_connection.fetchval.mock_calls:
        if call.args:
            calls.append(str(call.args[0]))

    for call in mock_db_connection.fetchrow.mock_calls:
        if call.args:
            calls.append(str(call.args[0]))

    assert mock_db_connection.execute.call_count >= 4

    # Check for specific SQL fragments in calls
    assert any("CREATE TABLE IF NOT EXISTS states_raw" in c for c in calls)
    # View creation SQL is multi-line, check for a substring
    assert any("CREATE VIEW states" in c for c in calls)
    assert any("DROP VIEW IF EXISTS states" in c for c in calls)
    assert any("CREATE TABLE IF NOT EXISTS events" in c for c in calls)

    # Hypertable is on states_raw now
    assert any("create_hypertable('states_raw'" in c for c in calls)
    assert any("create_hypertable('events'" in c for c in calls)

    # Check for initial count queries
    # SELECT count(*) FROM states (view) or states_raw?
    # writer.py: SELECT count(*) FROM {self.table_name_states} -> "states" view
    assert any("SELECT count(*) FROM states" in c for c in calls)
    assert any("SELECT count(*) FROM events" in c for c in calls)


@pytest.mark.asyncio
async def test_writer_enqueue_flush(writer, mock_db_connection):
    """Test enqueue and flush logic."""
    # Mock initial counts to 0
    mock_db_connection.fetchval.return_value = 0

    await writer.start()
    await asyncio.sleep(0.1)  # Let background init and counts finish

    # Reset side_effect for flush (or just ensure it works)
    mock_db_connection.execute.side_effect = None

    # Pre-populate map
    writer._entity_id_map["sensor.test"] = 1

    # Enqueue items
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 1.0,
            "attributes": {},
        }
    )
    assert len(writer._queue) == 1

    # Enqueue second item - this should trigger auto-flush task
    writer.enqueue(
        {
            "type": "event",
            "time": datetime.now(),
            "event_type": "test",
            "event_data": {},
        }
    )

    # Allow the loop to run the flush task
    await asyncio.sleep(0.1)

    # If auto-flush worked, queue should be empty
    # Verify DB calls now use COPY batches.
    assert mock_db_connection.copy_records_to_table.call_count >= 2

    # Verify stats
    assert writer._states_written == 1
    assert writer._events_written == 1


@pytest.mark.asyncio
async def test_writer_flush_uses_copy_for_states_and_events(writer, mock_db_connection):
    """Test that flush writes state/event batches via COPY."""
    mock_db_connection.fetchval.return_value = 0

    await writer.start()
    await asyncio.sleep(0.1)

    writer._entity_id_map["sensor.test"] = 1
    now = datetime(2023, 10, 27, 10, 0, 0, tzinfo=timezone.utc)

    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": now,
            "state": "on",
            "value": 1.0,
            "attributes": {"foo": "bar"},
        }
    )
    writer.enqueue(
        {
            "type": "event",
            "time": now,
            "event_type": "test",
            "event_data": {"bar": 1},
            "origin": "LOCAL",
        }
    )

    await writer._flush()

    assert mock_db_connection.copy_records_to_table.call_count == 2
    state_call = mock_db_connection.copy_records_to_table.call_args_list[0]
    event_call = mock_db_connection.copy_records_to_table.call_args_list[1]

    assert state_call.kwargs["table_name"] == "states_raw"
    assert state_call.kwargs["columns"] == [
        "time",
        "metadata_id",
        "state",
        "value",
        "attributes",
    ]
    assert state_call.kwargs["records"][0][1] == 1

    assert event_call.kwargs["table_name"] == writer.table_name_events
    assert event_call.kwargs["columns"] == [
        "time",
        "event_type",
        "event_data",
        "origin",
        "context_id",
        "context_user_id",
        "context_parent_id",
    ]
    assert event_call.kwargs["records"][0][1] == "test"


@pytest.mark.asyncio
async def test_writer_no_buffer_on_failure(writer, mock_pool, mock_db_connection):
    """Test that events are dropped when buffering is disabled."""
    writer.buffer_on_failure = False
    writer.batch_size = 1
    await writer.start()

    # Mock connection failure during flush
    mock_db_connection.copy_records_to_table.side_effect = Exception(
        "Connection failed"
    )
    mock_db_connection.fetchval.side_effect = Exception("Connection failed")
    mock_db_connection.fetchrow.side_effect = Exception("Connection failed")

    # Pre-populate map
    writer._entity_id_map["sensor.test"] = 1

    # Enqueue item
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 1.0,
            "attributes": {},
        }
    )

    # Flush
    await writer._flush()

    # Should be empty because it tried to flush, failed, and dropped it
    assert len(writer._queue) == 0
    assert writer._dropped_events == 1


@pytest.mark.asyncio
async def test_writer_buffer_on_failure(writer, mock_pool, mock_db_connection):
    """Test that events are buffered when buffering is enabled."""
    writer.buffer_on_failure = True
    writer.batch_size = 1
    await writer.start()

    # Mock connection failure during flush
    mock_db_connection.copy_records_to_table.side_effect = Exception(
        "Connection failed"
    )
    mock_db_connection.fetchval.side_effect = Exception("Connection failed")
    mock_db_connection.fetchrow.side_effect = Exception("Connection failed")

    # Pre-populate map
    writer._entity_id_map["sensor.test"] = 1

    # Enqueue item
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 1.0,
            "attributes": {},
        }
    )

    # Flush
    await writer._flush()

    # Should NOT be empty because it tried to flush, failed, and put it back
    assert len(writer._queue) == 1
    assert writer._queue[0]["value"] == 1.0


@pytest.mark.asyncio
async def test_writer_max_queue_size(writer):
    """Test that events are dropped when queue is full."""
    writer.max_queue_size = 2
    writer._queue = deque(maxlen=writer.max_queue_size)  # Re-init deque with new maxlen
    writer.batch_size = 100  # Prevent auto-flush
    writer._running = True

    # Pre-populate map
    writer._entity_id_map["sensor.test"] = 1

    # Fill queue
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 1.0,
            "attributes": {},
        }
    )
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 2.0,
            "attributes": {},
        }
    )
    assert len(writer._queue) == 2

    # Add one more, should be dropped
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 3.0,
            "attributes": {},
        }
    )
    assert len(writer._queue) == 2
    # dropped_events is not incremented by deque automatically, only if we manually check
    # But enqueue checks len vs max_queue_size?
    # Wait, enqueue uses self._queue.append(). Deque handles dropping.
    # So self._dropped_events is NOT incremented in enqueue anymore!
    # We removed that logic in writer.py.
    # So we should remove this assertion or check that it is NOT incremented (or check logs if we could)
    # assert writer._dropped_events == 1
    assert writer._queue[0]["value"] == 2.0
    assert writer._queue[1]["value"] == 3.0


@pytest.mark.asyncio
async def test_writer_get_db_stats(writer, mock_db_connection):
    """Test fetching DB stats."""
    await writer.start()

    # Mock return values
    # We need to mock the result of execute().scalar() and fetchone()

    # This is tricky because execute is called multiple times.
    # We can use side_effect to return different mocks based on query

    async def fetchval_side_effect(statement, *args):
        stmt_str = str(statement)
        if "hypertable_detailed_size" in stmt_str:
            return 1000
        if "after_compression_total_bytes" in stmt_str:
            return 100
        return 0

    async def fetchrow_side_effect(statement, *args):
        stmt_str = str(statement)
        if "timescaledb_information.chunks" in stmt_str:
            return {
                "total_chunks": 10,
                "compressed_chunks": 5,
                "uncompressed_chunks": 5,
            }
        if "hypertable_compression_stats" in stmt_str:
            return {
                "before_compression_total_bytes": 400,
                "after_compression_total_bytes": 100,
            }
        return None

    mock_db_connection.fetchval.side_effect = fetchval_side_effect
    mock_db_connection.fetchrow.side_effect = fetchrow_side_effect

    stats = await writer.get_db_stats()

    assert stats["states_total_size"] == 1000
    assert stats["states_total_chunks"] == 10
    assert stats["states_before_compression_total_bytes"] == 400
    assert stats["states_after_compression_total_bytes"] == 100


@pytest.mark.asyncio
async def test_writer_engine_creation_failure(hass, caplog):
    """Test failure during pool creation."""
    from custom_components.scribe.writer import ScribeWriter

    with patch(
        "custom_components.scribe.writer.asyncpg.create_pool",
        side_effect=Exception("Pool Error"),
    ):
        writer = ScribeWriter(
            hass=hass,
            db_url="postgresql://user:pass@host/db",
            chunk_interval="7 days",
            compress_after="60 days",
            record_states=True,
            record_events=True,
            batch_size=2,
            flush_interval=5,
            max_queue_size=10000,
            buffer_on_failure=True,
            table_name_states="states",
            table_name_events="events",
            ssl_root_cert=None,
            ssl_cert_file=None,
            ssl_key_file=None,
        )
        await writer.start()
        assert writer._pool is None
        assert writer._connected is False


@pytest.mark.asyncio
async def test_writer_init_db_failure(writer, mock_pool, mock_db_connection):
    """Test init_db failure."""
    mock_db_connection.execute.side_effect = Exception("DB Error")

    await writer.start()
    assert writer._connected is False


@pytest.mark.asyncio
async def test_writer_hypertable_failure(writer, mock_pool, mock_db_connection):
    """Test hypertable creation failure (should be logged but not fail init)."""
    # We need to allow initial table creation to succeed, but fail hypertable calls

    async def side_effect(statement, *args, **kwargs):
        stmt_str = str(statement)
        if "create_hypertable" in stmt_str:
            raise Exception("Hypertable Error")
        mock_res = MagicMock()
        mock_res.scalar.return_value = 0
        mock_res.fetchone.return_value = (0,)
        mock_res.fetchall.return_value = []
        return mock_res

    mock_db_connection.execute.side_effect = side_effect

    await writer.start()
    await asyncio.sleep(0.1)
    # Should still be connected because hypertable failure is caught
    assert writer._connected is True


@pytest.mark.asyncio
async def test_writer_get_db_stats_failure(writer, mock_db_connection):
    """Test stats fetching failure - should return partial stats with default values."""
    await writer.start()

    mock_db_connection.fetchval.side_effect = Exception("Stats Error")
    mock_db_connection.fetchrow.side_effect = Exception("Stats Error")

    stats = await writer.get_db_stats()
    # With PR #9, size stats functions return default 0 values even on failure
    # Chunk stats return empty dict on failure
    # So we expect size stats with 0 values
    assert stats.get("states_total_size", 0) == 0
    assert stats.get("events_total_size", 0) == 0


@pytest.mark.asyncio
async def test_writer_start_already_running(writer):
    """Test start when already running."""
    await writer.start()
    assert writer.running is True

    # Call start again
    with patch("custom_components.scribe.writer.asyncpg.create_pool") as mock_create:
        await writer.start()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_writer_stop_cancelled(writer):
    """Test stop handling CancelledError."""
    await writer.start()

    # Mock task to raise CancelledError when awaited
    async def mock_task():
        raise asyncio.CancelledError()

    writer._task = asyncio.create_task(mock_task())

    await writer.stop()
    assert writer.running is False


@pytest.mark.asyncio
async def test_writer_run_exception(writer):
    """Test exception in run loop."""
    writer._running = True
    writer.flush_interval = 0.01

    # Mock flush to raise exception then stop running
    async def side_effect():
        if writer._running:
            writer._running = False  # Stop loop after first error
            raise Exception("Loop Error")

    writer._flush = AsyncMock(side_effect=side_effect)

    await writer._run()
    # Should exit loop without crashing


@pytest.mark.asyncio
async def test_writer_init_db_no_engine(writer):
    """Test init_db with no engine."""
    writer._pool = None
    await writer.init_db()
    assert writer._connected is False


@pytest.mark.asyncio
async def test_writer_compression_policy_failure(writer, mock_pool, mock_db_connection):
    """Test compression policy failure."""

    async def side_effect(statement, *args, **kwargs):
        stmt_str = str(statement)
        if "add_compression_policy" in stmt_str:
            raise Exception("Policy Error")
        mock_res = MagicMock()
        mock_res.scalar.return_value = 0
        mock_res.fetchone.return_value = (0,)
        mock_res.fetchall.return_value = []
        return mock_res

    mock_db_connection.execute.side_effect = side_effect

    await writer.start()
    await asyncio.sleep(0.1)
    assert writer._connected is True


@pytest.mark.asyncio
async def test_writer_buffer_full_drop_oldest(writer, mock_pool, mock_db_connection):
    """Test dropping oldest events when buffer is full (buffer_on_failure=True)."""
    writer.buffer_on_failure = True
    writer.max_queue_size = 2
    writer._queue = deque(maxlen=writer.max_queue_size)  # Re-init deque
    writer.batch_size = 10  # Prevent auto-flush
    writer._pool = mock_pool
    writer._running = True

    # Pre-populate map
    writer._entity_id_map["sensor.test"] = 1

    # Fill queue
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 1.0,
            "attributes": {},
        }
    )
    writer.enqueue(
        {
            "type": "state",
            "entity_id": "sensor.test",
            "time": datetime.now(),
            "state": "on",
            "value": 2.0,
            "attributes": {},
        }
    )

    # Mock acquire() to return a context manager that adds an item
    # Use the existing acquire_ctx from mock_pool if possible, or create new
    acquire_ctx = AsyncMock()
    mock_pool.acquire.return_value = acquire_ctx

    async def mock_enter(*args, **kwargs):
        # Simulate concurrent add
        writer.enqueue(
            {
                "type": "state",
                "entity_id": "sensor.test",
                "time": datetime.now(),
                "state": "on",
                "value": 3.0,
                "attributes": {},
            }
        )
        return mock_db_connection

    acquire_ctx.__aenter__ = AsyncMock(side_effect=mock_enter)

    # Mock COPY to raise exception
    mock_db_connection.copy_records_to_table.side_effect = Exception("Flush Error")

    # Trigger flush manually
    await writer._flush()

    # Batch was [1, 2]. Queue became [3].
    # Re-buffer: [1, 2, 3]. Max size 2.
    # Should drop 1 (oldest). Result: [2, 3].

    assert len(writer._queue) == 2
    assert writer._queue[0]["value"] == 2.0
    assert writer._queue[1]["value"] == 3.0
    # assert writer._dropped_events == 1 # Again, not tracked manually anymore


@pytest.mark.asyncio
async def test_writer_get_db_stats_no_engine(writer):
    """Test get_db_stats with no engine."""
    writer._pool = None
    stats = await writer.get_db_stats()
    assert stats == {}


@pytest.mark.asyncio
async def test_writer_compression_enable_failure(writer, mock_pool, mock_db_connection):
    """Test compression enable failure."""

    async def side_effect(statement, *args, **kwargs):
        stmt_str = str(statement)
        if "timescaledb.compress" in stmt_str:
            raise Exception("Compression Enable Error")
        mock_res = MagicMock()
        mock_res.scalar.return_value = 0
        mock_res.fetchone.return_value = (0,)
        mock_res.fetchall.return_value = []
        return mock_res

    mock_db_connection.execute.side_effect = side_effect

    await writer.start()
    await asyncio.sleep(0.1)
    assert writer._connected is True


@pytest.mark.asyncio
async def test_writer_get_db_stats_connect_failure(writer):
    """Test get_db_stats connection failure - should return partial stats with default values."""
    await writer.start()

    # Mock acquire() to raise exception
    writer._pool.acquire.side_effect = Exception("Connect Error")

    stats = await writer.get_db_stats()
    # With PR #9, size stats functions return default 0 values even on failure
    assert stats.get("states_total_size", 0) == 0
    assert stats.get("events_total_size", 0) == 0


@pytest.mark.asyncio
async def test_writer_sanitizes_null_bytes(writer, mock_db_connection):
    """Test that null bytes are removed from strings."""
    # Start writer
    await writer.start()

    # Enqueue data with null bytes
    # Note: writers uses datetime in queue
    now = datetime(2023, 10, 27, 10, 0, 0, tzinfo=timezone.utc)

    # Pre-populate entity map to avoid lookup failure
    writer._entity_id_map["sensor.dirty"] = 1

    dirty_state = {
        "time": now,
        "entity_id": "sensor.dirty",
        "state": "bad\u0000value",
        "value": 1.0,
        "attributes": {"key": "nu\u0000ll"},
        "type": "state",
    }
    writer.enqueue(dirty_state)

    # Trigger flush manually
    await writer._flush()

    # Verify COPY call
    assert mock_db_connection.copy_records_to_table.called
    call_args = mock_db_connection.copy_records_to_table.call_args_list[0]
    args_list = call_args.kwargs["records"]

    # Verify the first item in the batch
    # Tuple: (time, metadata_id, state, value, attributes)
    item_state = args_list[0][2]
    item_attributes = args_list[0][4]

    # Null bytes should be removed
    assert "\u0000" not in item_state
    assert item_state == "badvalue"
    assert "\u0000" not in item_attributes
    # attributes are JSON strings now
    assert "bad\u0000value" not in item_attributes
    assert "nu\u0000ll" not in item_attributes


@pytest.mark.asyncio
async def test_writer_sanitizes_infinity(writer, mock_db_connection):
    """Test that Infinity and NaN are converted to None."""
    await writer.start()

    # Enqueue data with Infinity and NaN
    now = datetime(2023, 10, 27, 10, 0, 0, tzinfo=timezone.utc)

    # Pre-populate entity map
    writer._entity_id_map["sensor.infinity"] = 1

    dirty_state = {
        "time": now,
        "entity_id": "sensor.infinity",
        "state": "inf",
        "value": float("inf"),
        "attributes": {
            "pos_inf": float("inf"),
            "neg_inf": float("-inf"),
            "nan": float("nan"),
            "nested": [float("inf")],
        },
        "type": "state",
    }
    writer.enqueue(dirty_state)

    await writer._flush()

    assert mock_db_connection.copy_records_to_table.called
    call_args = mock_db_connection.copy_records_to_table.call_args_list[0]
    args_list = call_args.kwargs["records"]
    # Tuple: (time, metadata_id, state, value, attributes)
    item_value = args_list[0][3]
    item_attributes = args_list[0][4]

    # Value (float) should be None if it was infinity/nan
    assert item_value is None

    attributes = item_attributes

    assert attributes["pos_inf"] is None
    assert attributes["neg_inf"] is None
    assert attributes["nan"] is None
    assert attributes["nested"][0] is None
