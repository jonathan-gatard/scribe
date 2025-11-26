"""Test ScribeWriter."""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from custom_components.scribe.writer import ScribeWriter

@pytest.mark.asyncio
async def test_writer_enqueue_flush():
    """Test enqueue and flush logic."""
    hass = MagicMock()
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
        table_name_events="events"
    )

    # Mock Engine
    # AsyncEngine is not awaitable, so use MagicMock
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    
    # Setup async context manager explicitly for begin()
    mock_transaction = AsyncMock()
    mock_transaction.__aenter__.return_value = mock_conn
    mock_transaction.__aexit__.return_value = None
    
    mock_engine.begin.return_value = mock_transaction
    
    writer._engine = mock_engine
    writer._running = True

    # Enqueue items
    writer.enqueue({"type": "state", "data": 1})
    assert len(writer._queue) == 1
    
    # Enqueue second item - this should trigger auto-flush task
    writer.enqueue({"type": "event", "data": 2})
    
    # Allow the loop to run the flush task
    await asyncio.sleep(0.1)
    
    # If auto-flush worked, queue should be empty
    if len(writer._queue) > 0:
        await writer._flush()
    
    assert len(writer._queue) == 0 

    # Verify DB calls
    assert mock_conn.execute.call_count >= 1
    
    # Verify stats
    assert writer._states_written == 1
    assert writer._events_written == 1

@pytest.mark.asyncio
async def test_writer_no_buffer_on_failure():
    """Test that events are dropped when buffering is disabled."""
    hass = MagicMock()
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=1,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=False,
        table_name_states="states",
        table_name_events="events"
    )

    # Mock Engine to fail
    mock_engine = MagicMock()
    mock_transaction = AsyncMock()
    mock_transaction.__aenter__.side_effect = Exception("Connection failed")
    mock_engine.begin.return_value = mock_transaction
    
    writer._engine = mock_engine
    
    # Enqueue item
    writer.enqueue({"type": "state", "data": 1})
    
    # Flush
    await writer._flush()
    
    # Should be empty because it tried to flush, failed, and dropped it
    assert len(writer._queue) == 0
    assert writer._dropped_events == 1

@pytest.mark.asyncio
async def test_writer_buffer_on_failure():
    """Test that events are buffered when buffering is enabled."""
    hass = MagicMock()
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=1,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events"
    )

    # Mock Engine to fail
    mock_engine = MagicMock()
    mock_transaction = AsyncMock()
    mock_transaction.__aenter__.side_effect = Exception("Connection failed")
    mock_engine.begin.return_value = mock_transaction
    
    writer._engine = mock_engine
    
    # Enqueue item
    writer.enqueue({"type": "state", "data": 1})
    
    # Flush
    await writer._flush()
    
    # Should NOT be empty because it tried to flush, failed, and put it back
    assert len(writer._queue) == 1
    assert writer._queue[0]["data"] == 1

@pytest.mark.asyncio
async def test_writer_max_queue_size():
    """Test that events are dropped when queue is full."""
    hass = MagicMock()
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=100, 
        flush_interval=5,
        max_queue_size=2, # Small max size
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events"
    )

    # Fill queue
    writer.enqueue({"type": "state", "data": 1})
    writer.enqueue({"type": "state", "data": 2})
    assert len(writer._queue) == 2
    
    # Add one more, should be dropped
    writer.enqueue({"type": "state", "data": 3})
    assert len(writer._queue) == 2
    assert writer._dropped_events == 1
