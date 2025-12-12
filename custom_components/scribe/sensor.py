"""Sensor platform for Scribe.

This module exposes internal metrics of the Scribe integration as Home Assistant sensors.
These sensors allow users to monitor the health and performance of the database writer,
including queue size, write latency, and database storage usage.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorEntityDescription,
    SensorDeviceClass,
)
from homeassistant.const import UnitOfInformation, PERCENTAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import (
    DOMAIN, 
    CONF_ENABLE_STATS_IO,
    DEFAULT_ENABLE_STATS_IO,
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Scribe sensors.
    
    Retrieves the writer instance and coordinators from hass.data and creates
    the sensor entities based on enabled statistics types.
    """
    data = hass.data[DOMAIN][entry.entry_id]
    writer = data["writer"]
    chunk_coordinator = data.get("chunk_coordinator")
    size_coordinator = data.get("size_coordinator")
    
    entities = []
    
    # IO Statistics Sensors (always-on, real-time from writer)
    enable_stats_io = entry.options.get(CONF_ENABLE_STATS_IO, entry.data.get(CONF_ENABLE_STATS_IO, DEFAULT_ENABLE_STATS_IO))
    if enable_stats_io:
        entities.extend([
            ScribeStatesWrittenSensor(writer, entry),
            ScribeEventsWrittenSensor(writer, entry),
            ScribeBufferSizeSensor(writer, entry),
            ScribeWriteDurationSensor(writer, entry),
        ])
    
    # Chunk Statistics Sensors (from chunk_coordinator)
    if chunk_coordinator:
        entities.extend([
            # States table chunks
            ScribeStatsTotalChunksSensor(chunk_coordinator, entry),
            ScribeStatsCompressedChunksSensor(chunk_coordinator, entry),
            ScribeStatsUncompressedChunksSensor(chunk_coordinator, entry),
            # Events table chunks
            ScribeEventsTotalChunksSensor(chunk_coordinator, entry),
            ScribeEventsCompressedChunksSensor(chunk_coordinator, entry),
            ScribeEventsUncompressedChunksSensor(chunk_coordinator, entry),
        ])
    
    # Size Statistics Sensors (from size_coordinator)
    if size_coordinator:
        entities.extend([
            # States table sizes
            ScribeStatsTotalSizeSensor(size_coordinator, entry),
            ScribeStatsCompressedSizeSensor(size_coordinator, entry),
            ScribeStatsUncompressedSizeSensor(size_coordinator, entry),
            # Events table sizes
            ScribeEventsTotalSizeSensor(size_coordinator, entry),
            ScribeEventsCompressedSizeSensor(size_coordinator, entry),
            ScribeEventsUncompressedSizeSensor(size_coordinator, entry),
            # Ratio
            # Ratio
            ScribeStatesCompressionRatioSensor(size_coordinator, entry),
            ScribeEventsCompressionRatioSensor(size_coordinator, entry),
        ])
    
    async_add_entities(entities, True)

class ScribeSensor(SensorEntity):
    """Base class for Scribe sensors.
    
    Directly polls the writer instance for real-time metrics.
    """

    _attr_has_entity_name = True

    def __init__(self, writer, entry):
        """Initialize the sensor."""
        self._writer = writer
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self.entity_description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Scribe",
            "manufacturer": "Jonathan Gatard",
        }

    @property
    def available(self) -> bool:
        """Return True if writer is running."""
        return self._writer.running

class ScribeCoordinatorSensor(CoordinatorEntity, SensorEntity):
    """Base class for Scribe coordinator sensors.
    
    Uses the DataUpdateCoordinator to fetch data, suitable for expensive queries
    like database size and compression stats.
    """
    
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, name):
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Scribe",
            "manufacturer": "Jonathan Gatard",
        }

# =============================================
# STATES TABLE SENSORS
# =============================================

class ScribeStatsTotalSizeSensor(ScribeCoordinatorSensor):
    """Sensor for States total size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_total_size", "States Total Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:database"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_total_size", 0)


class ScribeStatsCompressedSizeSensor(ScribeCoordinatorSensor):
    """Sensor for States compressed size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compressed_size", "States Compressed Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-variant"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_compressed_size", 0)


class ScribeStatsUncompressedSizeSensor(ScribeCoordinatorSensor):
    """Sensor for States uncompressed size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_uncompressed_size", "States Uncompressed Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-variant-closed"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_uncompressed_size", 0)


class ScribeStatsTotalChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States total chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_total_chunks", "States Total Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cube-outline"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_total_chunks", 0)


class ScribeStatsCompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States compressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compressed_chunks", "States Compressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-down"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_compressed_chunks", 0)


class ScribeStatsUncompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States uncompressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_uncompressed_chunks", "States Uncompressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-up"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_uncompressed_chunks", 0)


# =============================================
# EVENTS TABLE SENSORS
# =============================================

class ScribeEventsTotalSizeSensor(ScribeCoordinatorSensor):
    """Sensor for Events total size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_total_size", "Events Total Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:database"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_total_size", 0)


class ScribeEventsCompressedSizeSensor(ScribeCoordinatorSensor):
    """Sensor for Events compressed size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_compressed_size", "Events Compressed Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-variant"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_compressed_size", 0)


class ScribeEventsUncompressedSizeSensor(ScribeCoordinatorSensor):
    """Sensor for Events uncompressed size."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_uncompressed_size", "Events Uncompressed Size")
        self._attr_native_unit_of_measurement = UnitOfInformation.BYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_suggested_display_precision = 0
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-variant-closed"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_uncompressed_size", 0)


class ScribeEventsTotalChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events total chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_total_chunks", "Events Total Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cube-outline"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_total_chunks", 0)


class ScribeEventsCompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events compressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_compressed_chunks", "Events Compressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-down"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_compressed_chunks", 0)


class ScribeEventsUncompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events uncompressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_uncompressed_chunks", "Events Uncompressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-up"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_uncompressed_chunks", 0)


class ScribeStatesCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for States Compression Ratio."""
    
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ratio"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compression_ratio", "States Compression Ratio")

    @property
    def native_value(self):
        data = self.coordinator.data
        # Use TOTAL size as base (compressed + uncompressed chunks), but ratio is usually defined on compressed vs uncompressed SIZE.
        # Actually, "states_uncompressed_size" is size of uncompressed chunks.
        # "states_compressed_size" is size of compressed chunks.
        # This doesn't directly give "original size vs compressed size" unless we know original size of compressed chunks.
        # However, typically people compare: 1 - (compressed_size / (compressed_size_if_it_was_uncompressed + uncompressed_size))
        # Wait, if `states_compressed_size` is the INVALID compressed size (actual disk usage), and we don't know the original size, valid ratio is hard.
        # BUT, usually database compression ratio is: Total Size Before Compression / Total Size After Compression.
        # Here `states_total_size` = compressed_size + uncompressed_size.
        # We probably want (Uncompressed_Size_of_Compressed_Chunks + Uncompressed_Size_of_Uncompressed_Chunks) / Total_Size.
        # But we don't have "Uncompressed_Size_of_Compressed_Chunks".
        # Let's assume the user just wants the ratio of space saved?
        # Or simple ratio: compressed_size / total_size?
        
        # Let's look at previous implementation: (1 - (compressed / uncompressed)) * 100.
        # This implies we had access to uncompressed size.
        # In writer.py: 
        # compressed_size IS the size of chunks that are compressed.
        # uncompressed_size IS the size of chunks that are NOT compressed.
        # This means we CANNOT calculate true compression ratio without Knowing how big the compressed chunks WERE.
        
        # IF we assume the user just wants to know what % of the database is compressed vs uncompressed:
        # That's different.
        
        # Let's re-read writer.py carefully.
        # It calls `pg_total_relation_size`.
        
        # Let's stick to simple "What % of total size is compressed data?" or wait...
        # If user sees "Compression Ratio", they expect "How much did I save?".
        # Without "original size" metric, we can't calculate that accurately.
        # BUT, standard TimescaleDB views might offer `before_compression_total_bytes` etc.
        # `timescaledb_information.chunks` does NOT seem to have that easily?
        # Actually, `hypertable_compression_stats` view usually has `compression_ratio`.
        # writer.py is querying `chunks` view manually.
        
        # Since I can't change writer.py easily right now without checking DB docs, 
        # I will implement a placeholder or best-effort.
        # Actually, let's just use what we have: 
        # Maybe ratio of Compressed Chunks (count) to Total Chunks?
        # No, user asked "compression ratio".
        
        # Wait, the previous code used `states_uncompressed_total_bytes` which implied someone thought we had it.
        # Let's check `writer.py` again. It returns: `states_total_size`, `states_compressed_size`, `states_uncompressed_size`.
        
        # I'll stick to: compressed_size vs total_size? No that's "Compression Levels".
        # Let's return 0 for now if we can't calculate, OR just fix the keys and see.
        
        # ACTUALLY, usually people assume `states_uncompressed_size` is the "Raw" size.
        # But here it clearly says: `WHERE NOT is_compressed`. So it's the size of the *currently uncompressed* chunks (hot data).
        
        # Reviewing `writer.py`... 
        # It seems we are missing the "Uncompressed size of compressed chunks" metric.
        # I will document this limitation if necessary, but for now I will use:
        # (states_compressed_size + states_uncompressed_size) as total usage.
        # If I can't calculate savings, maybe I should just remove it?
        # User REPLICITLY asked for it. 
        
        # Let's try to infer if I can enable `hypertable_compression_stats` in writer.py later.
        # For now, I will use `states_compressed_size` / `states_total_size` as "Compressed Percentage" (Storage Efficiency?)
        # NO, Compression Ratio is typically > 1.0 (e.g. 10x) or Percentage Saved (90%).
        
        # Let's use `timescaledb_information.hypertables` or similar in a future update.
        # For now, I'll return `None` (unknown) but with the correct keys so it doesn't crash, 
        # AND I'll implement `ScribeStatesCompressionRatioSensor` and `ScribeEventsCompressionRatioSensor`.
        # I'll add a TODO/Comment.
        
        # Re-reading user prompt: "compresssion ratio est unknow (question : il s'applique sur quelle table ?)"
        # The user just wants it to work.
        
        # Let's look at `writer.py` one more time.
        # `get_states_size_stats` queries `timescaledb_information.chunks`.
        # It sums `pg_total_relation_size`.
        
        # If I cannot calculate it, I should maybe remove it or change to "Percent Compressed"?
        # But I will fix the keys `states_compressed_size` and `states_uncompressed_size` first.
        # Maybe the previous dev (me) thought `uncompressed_size` meant "Total Uncompressed Size"?
        # If so, the formula `1 - (compressed / uncompressed)` would be "Savings".
        
        # I will use the keys that EXIST: `states_compressed_size` and `states_total_size`.
        # And I'll leave the logic as `unknown` if 0, but use the correct keys.
        # Wait, I'll assume `states_uncompressed_size` key in `data` was meant to be `states_uncompressed_size` from writer.
        
        data = self.coordinator.data
        
        # Using correct keys from writer.py
        uncompressed = data.get("states_uncompressed_size", 0)
        compressed = data.get("states_compressed_size", 0)
        total = data.get("states_total_size", 0)
        
        if not total:
            return None
            
        # Returning % of storage that is compressed for now?
        # Or just return None if I can't calculate Ratio?
        # User wants "Compression Ratio".
        
        # I'll implement valid sensors with correct keys, but maybe set state to None if logic is impossible.
        # BUT, I'll change the implementation to:
        # separate sensors for States and Events.
        
        return None 

class ScribeStatesCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for States Compression Ratio."""
    
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ratio"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compression_ratio", "States Compression Ratio")

    @property
    def native_value(self):
        data = self.coordinator.data
        before = data.get("states_before_compression_total_bytes", 0)
        after = data.get("states_after_compression_total_bytes", 0)
        
        if not before:
            return None
            
        # Calculate percentage saved (1 - compressed/original)
        return round((1 - (after / before)) * 100, 1)

class ScribeEventsCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for Events Compression Ratio."""
    
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ratio"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_compression_ratio", "Events Compression Ratio")

    @property
    def native_value(self):
        data = self.coordinator.data
        before = data.get("events_before_compression_total_bytes", 0)
        after = data.get("events_after_compression_total_bytes", 0)
        
        if not before:
            return None

        # Calculate percentage saved
        return round((1 - (after / before)) * 100, 1)


class ScribeStatesWrittenSensor(ScribeSensor):
    """Sensor for total states written."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="states_written",
            name="States Written",
            icon="mdi:database-plus",
            state_class=SensorStateClass.TOTAL_INCREASING,
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._states_written

class ScribeEventsWrittenSensor(ScribeSensor):
    """Sensor for total events written."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="events_written",
            name="Events Written",
            icon="mdi:database-plus",
            state_class=SensorStateClass.TOTAL_INCREASING,
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._events_written

class ScribeBufferSizeSensor(ScribeSensor):
    """Sensor for current buffer size."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="buffer_size",
            name="Buffer Size",
            icon="mdi:buffer",
            state_class=SensorStateClass.MEASUREMENT,
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor.
        
        No lock needed as we are running in the same thread (asyncio).
        """
        return len(self._writer._queue)

class ScribeWriteDurationSensor(ScribeSensor):
    """Sensor for last write duration."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="write_duration",
            name="Last Write Duration",
            icon="mdi:timer-sand",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="ms",
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return round(self._writer._last_write_duration * 1000, 2)
