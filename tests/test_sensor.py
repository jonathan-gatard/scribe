"""Test Scribe sensors."""
import pytest
from unittest.mock import MagicMock
from custom_components.scribe.sensor import (
    ScribeStatesWrittenSensor,
    ScribeEventsWrittenSensor,
    ScribeBufferSizeSensor,
    ScribeWriteDurationSensor,
    ScribeStatsTotalSizeSensor,
    ScribeStatsCompressedSizeSensor,
    ScribeStatsUncompressedSizeSensor,
    ScribeStatsTotalChunksSensor,
    ScribeStatsCompressedChunksSensor,
    ScribeStatsUncompressedChunksSensor,
    ScribeEventsTotalSizeSensor,
    ScribeEventsCompressedSizeSensor,
    ScribeEventsUncompressedSizeSensor,
    ScribeEventsTotalChunksSensor,
    ScribeEventsCompressedChunksSensor,
    ScribeEventsUncompressedChunksSensor,
)

@pytest.mark.asyncio
async def test_writer_sensors():
    """Test sensor values directly from writer."""
    writer = MagicMock()
    writer.running = True
    writer._states_written = 10
    writer._events_written = 20
    writer._queue = [1, 2, 3]
    writer._last_write_duration = 0.5
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    # Test States Written
    sensor = ScribeStatesWrittenSensor(writer, entry)
    assert sensor.native_value == 10
    assert sensor.available is True
    
    # Test Events Written
    sensor = ScribeEventsWrittenSensor(writer, entry)
    assert sensor.native_value == 20
    
    # Test Buffer Size
    sensor = ScribeBufferSizeSensor(writer, entry)
    assert sensor.native_value == 3
    
    # Test Write Duration
    sensor = ScribeWriteDurationSensor(writer, entry)
    assert sensor.native_value == 0.5

@pytest.mark.asyncio
async def test_chunk_coordinator_sensors():
    """Test chunk coordinator sensors."""
    coordinator = MagicMock()
    coordinator.data = {
        "states_total_chunks": 10,
        "states_compressed_chunks": 8,
        "states_uncompressed_chunks": 2,
        "events_total_chunks": 20,
        "events_compressed_chunks": 15,
        "events_uncompressed_chunks": 5,
    }
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    # States
    assert ScribeStatsTotalChunksSensor(coordinator, entry).native_value == 10
    assert ScribeStatsCompressedChunksSensor(coordinator, entry).native_value == 8
    assert ScribeStatsUncompressedChunksSensor(coordinator, entry).native_value == 2
    
    # Events
    assert ScribeEventsTotalChunksSensor(coordinator, entry).native_value == 20
    assert ScribeEventsCompressedChunksSensor(coordinator, entry).native_value == 15
    assert ScribeEventsUncompressedChunksSensor(coordinator, entry).native_value == 5

@pytest.mark.asyncio
async def test_size_coordinator_sensors():
    """Test size coordinator sensors."""
    coordinator = MagicMock()
    coordinator.data = {
        "states_total_size": 1000,
        "states_compressed_size": 800,
        "states_uncompressed_size": 200,
        "events_total_size": 2000,
        "events_compressed_size": 1500,
        "events_uncompressed_size": 500,
    }
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    # States
    assert ScribeStatsTotalSizeSensor(coordinator, entry).native_value == 1000
    assert ScribeStatsCompressedSizeSensor(coordinator, entry).native_value == 800
    assert ScribeStatsUncompressedSizeSensor(coordinator, entry).native_value == 200
    
    # Events
    assert ScribeEventsTotalSizeSensor(coordinator, entry).native_value == 2000
    assert ScribeEventsCompressedSizeSensor(coordinator, entry).native_value == 1500
    assert ScribeEventsUncompressedSizeSensor(coordinator, entry).native_value == 500

@pytest.mark.asyncio
async def test_async_setup_entry_statistics(hass):
    """Test setup entry with statistics enabled."""
    from custom_components.scribe.sensor import async_setup_entry
    from custom_components.scribe.const import (
        DOMAIN, 
        CONF_ENABLE_STATS_IO,
        CONF_ENABLE_STATS_CHUNK,
        CONF_ENABLE_STATS_SIZE
    )
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    # Enable all stats
    entry.options = {
        CONF_ENABLE_STATS_IO: True,
        CONF_ENABLE_STATS_CHUNK: True,
        CONF_ENABLE_STATS_SIZE: True
    }
    # Fallback for .get on entry.data
    entry.data = {}
    
    writer = MagicMock()
    chunk_coordinator = MagicMock()
    size_coordinator = MagicMock()
    
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                "writer": writer, 
                "chunk_coordinator": chunk_coordinator,
                "size_coordinator": size_coordinator
            }
        }
    }
    
    async_add_entities = MagicMock()
    
    await async_setup_entry(hass, entry, async_add_entities)
    
    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    
    # 4 IO sensors + 6 Chunk sensors + 6 Size sensors = 16
    assert len(entities) == 16
