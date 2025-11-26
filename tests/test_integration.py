"""Integration tests for Scribe."""
import pytest
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, State
from custom_components.scribe.writer import ScribeWriter
from custom_components.scribe import async_setup_entry
from custom_components.scribe.const import DOMAIN

@pytest.mark.asyncio
async def test_event_to_db_write():
    """Test that a Home Assistant event results in a DB write."""
    hass = MagicMock()
    hass.data = {}
    
    # Mock Config Entry
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        "db_url": "postgresql://user:pass@host/db",
        "record_states": True,
        "record_events": True
    }
    entry.options = {}
    
    # Mock Writer in hass.data (simulating setup)
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=1, # Flush immediately
        flush_interval=60,
        max_queue_size=100,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events"
    )
    
    # Mock Engine & Connection
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_transaction = AsyncMock()
    mock_transaction.__aenter__.return_value = mock_conn
    mock_transaction.__aexit__.return_value = None
    mock_engine.begin.return_value = mock_transaction
    
    writer._engine = mock_engine
    writer._running = True
    
    # Simulate Event
    now = datetime.now()
    event_data = {
        "entity_id": "sensor.test",
        "new_state": State("sensor.test", "123", {"unit": "W"}, last_updated=now),
        "old_state": None
    }
    # Event constructor accepts time_fired
    event = Event(EVENT_STATE_CHANGED, event_data, time_fired=now)
    
    # Manually trigger the logic that would happen in handle_event
    # Since we can't easily invoke the inner function defined in async_setup_entry without complex setup,
    # we will test the writer's enqueue and flush directly with data derived from the event,
    # which mimics the integration logic.
    
    # 1. Transform Event to Data (Logic from __init__.py)
    state_data = {
        "type": "state",
        "time": event.data["new_state"].last_updated,
        "entity_id": event.data["entity_id"],
        "state": event.data["new_state"].state,
        "value": float(event.data["new_state"].state),
        "attributes": json.dumps(dict(event.data["new_state"].attributes), default=str),
    }
    
    # 2. Enqueue
    writer.enqueue(state_data)
    
    # 3. Flush (triggered by batch_size=1)
    await asyncio.sleep(0.1)
    if len(writer._queue) > 0:
        await writer._flush()
        
    # 4. Verify DB Write
    assert mock_conn.execute.call_count == 1
    call_args = mock_conn.execute.call_args
    sql_query = str(call_args[0][0])
    params = call_args[0][1]
    
    assert "INSERT INTO states" in sql_query
    assert len(params) == 1
    assert params[0]["entity_id"] == "sensor.test"
    assert params[0]["value"] == 123.0
    assert params[0]["state"] == "123"
    assert "unit" in params[0]["attributes"]

@pytest.mark.asyncio
async def test_generic_event_to_db_write():
    """Test that a generic Home Assistant event results in a DB write."""
    hass = MagicMock()
    
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=1,
        flush_interval=60,
        max_queue_size=100,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events"
    )
    
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_transaction = AsyncMock()
    mock_transaction.__aenter__.return_value = mock_conn
    mock_transaction.__aexit__.return_value = None
    mock_engine.begin.return_value = mock_transaction
    writer._engine = mock_engine
    writer._running = True
    
    now = datetime.now()
    event = Event("test_event", {"some": "data"}, time_fired=now)
    
    event_data = {
        "type": "event",
        "time": event.time_fired,
        "event_type": event.event_type,
        "event_data": json.dumps(event.data, default=str),
        "origin": str(event.origin),
        "context_id": event.context.id,
        "context_user_id": event.context.user_id,
        "context_parent_id": event.context.parent_id,
    }
    
    writer.enqueue(event_data)
    
    await asyncio.sleep(0.1)
    if len(writer._queue) > 0:
        await writer._flush()
        
    assert mock_conn.execute.call_count == 1
    call_args = mock_conn.execute.call_args
    sql_query = str(call_args[0][0])
    params = call_args[0][1]
    
    assert "INSERT INTO events" in sql_query
    assert params[0]["event_type"] == "test_event"
    assert '{"some": "data"}' in params[0]["event_data"]
