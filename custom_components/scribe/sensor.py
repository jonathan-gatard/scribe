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
        try:
            val = self.coordinator.data.get("states_total_size", 0)
            return val
        except Exception:
            return None

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
        try:
            return self.coordinator.data.get("states_compressed_size", 0)
        except Exception:
            return None


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
        try:
            return self.coordinator.data.get("states_uncompressed_size", 0)
        except Exception:
            return None


class ScribeStatsTotalChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States total chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_total_chunks", "States Total Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cube-outline"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("states_total_chunks", 0)
        except Exception:
            return None


class ScribeStatsCompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States compressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compressed_chunks", "States Compressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-down"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("states_compressed_chunks", 0)
        except Exception:
            return None


class ScribeStatsUncompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for States uncompressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_uncompressed_chunks", "States Uncompressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-up"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("states_uncompressed_chunks", 0)
        except Exception:
            return None


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
        try:
            return self.coordinator.data.get("events_total_size", 0)
        except Exception:
            return None


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
        try:
            return self.coordinator.data.get("events_compressed_size", 0)
        except Exception:
            return None


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
        try:
            return self.coordinator.data.get("events_uncompressed_size", 0)
        except Exception:
            return None


class ScribeEventsTotalChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events total chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_total_chunks", "Events Total Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cube-outline"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("events_total_chunks", 0)
        except Exception:
            return None


class ScribeEventsCompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events compressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_compressed_chunks", "Events Compressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-down"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("events_compressed_chunks", 0)
        except Exception:
            return None


class ScribeEventsUncompressedChunksSensor(ScribeCoordinatorSensor):
    """Sensor for Events uncompressed chunks."""
    
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_uncompressed_chunks", "Events Uncompressed Chunks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:package-up"

    @property
    def native_value(self):
        try:
            return self.coordinator.data.get("events_uncompressed_chunks", 0)
        except Exception:
            return None


 

class ScribeStatesCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for States Compression Ratio."""
    
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:percent"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "states_compression_ratio", "States Compression Ratio")

    @property
    def native_value(self):
        try:
            data = self.coordinator.data
            before = data.get("states_before_compression_total_bytes", 0)
            after = data.get("states_after_compression_total_bytes", 0)
            
            if not before:
                return None
                
            # Calculate percentage saved (1 - compressed/original)
            return round((1 - (after / before)) * 100, 1)
        except Exception:
            return None

class ScribeEventsCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for Events Compression Ratio."""
    
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:percent"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "events_compression_ratio", "Events Compression Ratio")

    @property
    def native_value(self):
        try:
            data = self.coordinator.data
            before = data.get("events_before_compression_total_bytes", 0)
            after = data.get("events_after_compression_total_bytes", 0)
            
            if not before:
                return None

            # Calculate percentage saved
            return round((1 - (after / before)) * 100, 1)
        except Exception:
            return None


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
        try:
            return self._writer._states_written
        except Exception:
            return 0

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
        try:
            return self._writer._events_written
        except Exception:
            return 0

class ScribeBufferSizeSensor(ScribeSensor):
    """Sensor for current buffer size."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="buffer_size",
            name="Buffer Size",
            icon="mdi:memory",
            state_class=SensorStateClass.MEASUREMENT,
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor.
        
        No lock needed as we are running in the same thread (asyncio).
        """
        try:
            return len(self._writer._queue)
        except Exception:
            return 0

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
        try:
             return round(self._writer._last_write_duration * 1000, 2)
        except Exception:
             return 0.0
