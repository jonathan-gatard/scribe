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
from homeassistant.const import UnitOfInformation

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
    assert sensor.native_value == 500.0

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
async def test_adaptive_units():
    """Test adaptive units logic for size sensors."""
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    # 1. Test kB (< 1 MB)
    # 500 kB = 512000 B
    coordinator.data = {"states_total_size": 512000}
    sensor = ScribeStatsTotalSizeSensor(coordinator, entry)
    assert sensor.native_value == 500
    assert sensor.native_unit_of_measurement == UnitOfInformation.KILOBYTES
    assert sensor.suggested_display_precision == 0

    # 2. Test MB (< 1 GB)
    # 10.5 MB = 11010048 B
    coordinator.data = {"states_total_size": 11010048}
    sensor = ScribeStatsTotalSizeSensor(coordinator, entry)
    assert sensor.native_value == 10.5
    assert sensor.native_unit_of_measurement == UnitOfInformation.MEGABYTES
    assert sensor.suggested_display_precision == 1

    # 3. Test GB (>= 1 GB)
    # 2.45 GB = 2630667468.8 B -> 2630667469 B
    coordinator.data = {"states_total_size": 2630667469}
    sensor = ScribeStatsTotalSizeSensor(coordinator, entry)
    assert sensor.native_value == 2.45
    assert sensor.native_unit_of_measurement == UnitOfInformation.GIGABYTES
    assert sensor.suggested_display_precision == 2

@pytest.mark.asyncio
async def test_size_coordinator_sensors():
    """Test size coordinator sensors (using MB range)."""
    coordinator = MagicMock()
    # Use values that result in clean MB numbers (approx)
    # 10.5 MB for example
    mb = 1024 * 1024
    
    coordinator.data = {
        "states_total_size": 10.5 * mb,
        "states_compressed_size": 8.1 * mb,
        "states_uncompressed_size": 2.4 * mb,
        "events_total_size": 20.0 * mb,
        "events_compressed_size": 15.0 * mb,
        "events_uncompressed_size": 5.0 * mb,
    }
    
    entry = MagicMock()
    entry.entry_id = "test_entry"
    
    # States
    s1 = ScribeStatsTotalSizeSensor(coordinator, entry)
    assert s1.native_value == 10.5
    assert s1.native_unit_of_measurement == UnitOfInformation.MEGABYTES
    
    s2 = ScribeStatsCompressedSizeSensor(coordinator, entry)
    assert s2.native_value == 8.1

    s3 = ScribeStatsUncompressedSizeSensor(coordinator, entry)
    assert s3.native_value == 2.4
    
    # Events
    e1 = ScribeEventsTotalSizeSensor(coordinator, entry)
    assert e1.native_value == 20.0
    
    e2 = ScribeEventsCompressedSizeSensor(coordinator, entry)
    assert e2.native_value == 15.0
    
    e3 = ScribeEventsUncompressedSizeSensor(coordinator, entry)
    assert e3.native_value == 5.0

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
    
    # 4 IO - Original
    # 2 Rate - New
    # 6 Chunk - Enabled
    # 6 Size - Enabled
    # 2 Ratio - Enabled
    # Total = 20
    assert len(entities) == 20
