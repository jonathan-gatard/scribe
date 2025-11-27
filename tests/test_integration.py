"""Integration tests for Scribe."""
import os
import pytest
import asyncio
import asyncpg
from datetime import datetime
from custom_components.scribe.writer import ScribeWriter

# Get DB URL from env or skip
DB_URL = os.getenv("SCRIBE_INTEGRATION_TEST_DB_URL")

@pytest.fixture(autouse=True)
def mock_create_async_engine():
    """Override the mock to use the real engine for integration tests."""
    yield

@pytest.mark.skipif(not DB_URL, reason="Integration test DB URL not set")
@pytest.mark.asyncio
async def test_integration_write_and_read(hass, socket_enabled):
    """Test writing to a real database and reading back."""
    
    # 1. Setup Writer
    writer = ScribeWriter(
        hass=hass,
        db_url=DB_URL,
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=1, # Write immediately
        flush_interval=1,
        max_queue_size=100,
        buffer_on_failure=False,
        table_name_states="states",
        table_name_events="events"
    )
    
    await writer.start()
    
    try:
        # 2. Write Data
        # State
        writer.enqueue({
            "type": "state",
            "entity_id": "sensor.integration_test",
            "state": "123.45",
            "attributes": '{"unit": "C"}',
            "time": datetime.now(),
            "value": 123.45
        })
        
        # Event
        writer.enqueue({
            "type": "event",
            "event_type": "integration_event",
            "event_data": '{"foo": "bar"}',
            "time": datetime.now(),
            "origin": "LOCAL",
            "context_id": "ctx_1",
            "context_user_id": "user_1",
            "context_parent_id": "parent_1"
        })
        
        # Wait for flush
        await asyncio.sleep(2)
        
        # 3. Verify with direct DB connection
        conn = await asyncpg.connect(DB_URL)
        
        try:
            # Check States
            row = await conn.fetchrow("SELECT * FROM states WHERE entity_id = 'sensor.integration_test'")
            assert row is not None
            assert row["state"] == "123.45"
            assert row["value"] == 123.45
            
            # Check Events
            row = await conn.fetchrow("SELECT * FROM events WHERE event_type = 'integration_event'")
            assert row is not None
            assert "bar" in row["event_data"]
            
        finally:
            await conn.close()
            
    finally:
        await writer.stop()

@pytest.mark.skipif(not DB_URL, reason="Integration test DB URL not set")
@pytest.mark.asyncio
async def test_integration_compression_setup(hass, socket_enabled):
    """Test that compression policies are correctly set up."""
    writer = ScribeWriter(
        hass=hass,
        db_url=DB_URL,
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=10,
        flush_interval=5,
        max_queue_size=100,
        buffer_on_failure=False,
        table_name_states="states",
        table_name_events="events"
    )
    
    await writer.start()
    await writer.stop()
    
    conn = await asyncpg.connect(DB_URL)
    try:
        # Check if compression is enabled on hypertables
        # We query timescaledb_information.hypertables or similar
        # But simpler: check if compression stats view has entries or just check pg_settings/config
        
        # Let's check timescaledb_information.jobs for compression policies
        rows = await conn.fetch("SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'")
        # We expect at least 2 jobs (states and events)
        assert len(rows) >= 2
        
    finally:
        await conn.close()
