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
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, CONF_ENABLE_STATISTICS, DEFAULT_ENABLE_STATISTICS

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Scribe sensors.
    
    Retrieves the writer instance and coordinator from hass.data and creates
    the sensor entities.
    """
    writer = hass.data[DOMAIN][entry.entry_id]["writer"]
    coordinator = hass.data[DOMAIN][entry.entry_id].get("coordinator")
    
    entities = [
        ScribeStatesWrittenSensor(writer, entry),
        ScribeEventsWrittenSensor(writer, entry),
        ScribeBufferSizeSensor(writer, entry),
        ScribeWriteDurationSensor(writer, entry),
    ]
    
    # Add Statistics Sensors if enabled
    # These sensors rely on the DataUpdateCoordinator to fetch data periodically
    if entry.options.get(CONF_ENABLE_STATISTICS, DEFAULT_ENABLE_STATISTICS) and coordinator:
        entities.extend([
            ScribeStatesSizeBeforeCompressionSensor(coordinator, entry, "states_size_before_compression", "States Size (Before Compression)"),
            ScribeStatesSizeAfterCompressionSensor(coordinator, entry, "states_size_after_compression", "States Size (After Compression)"),
            ScribeEventsSizeBeforeCompressionSensor(coordinator, entry, "events_size_before_compression", "Events Size (Before Compression)"),
            ScribeEventsSizeAfterCompressionSensor(coordinator, entry, "events_size_after_compression", "Events Size (After Compression)"),
            ScribeCompressionRatioSensor(coordinator, entry, "states_compression", "States Compression Ratio"),
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

class ScribeStatesSizeBeforeCompressionSensor(ScribeCoordinatorSensor):
    """Sensor for States Table size (Before Compression)."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:database-outline"

    @property
    def native_value(self):
        # Formula: (Total Size - Compressed Bytes) + Uncompressed Bytes
        total_size = self.coordinator.data.get("states_size_bytes", 0) or 0
        compressed_bytes = self.coordinator.data.get("states_compressed_total_bytes", 0) or 0
        uncompressed_bytes = self.coordinator.data.get("states_uncompressed_total_bytes", 0) or 0
        
        # If no compression, total_size is the uncompressed size
        if compressed_bytes == 0:
            return total_size
            
        return (total_size - compressed_bytes) + uncompressed_bytes

class ScribeStatesSizeAfterCompressionSensor(ScribeCoordinatorSensor):
    """Sensor for States Table size (After Compression)."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:database"

    @property
    def native_value(self):
        return self.coordinator.data.get("states_size_bytes", 0)

class ScribeEventsSizeBeforeCompressionSensor(ScribeCoordinatorSensor):
    """Sensor for Events Table size (Before Compression)."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:database-outline"

    @property
    def native_value(self):
        # Formula: (Total Size - Compressed Bytes) + Uncompressed Bytes
        total_size = self.coordinator.data.get("events_size_bytes", 0) or 0
        compressed_bytes = self.coordinator.data.get("events_compressed_total_bytes", 0) or 0
        uncompressed_bytes = self.coordinator.data.get("events_uncompressed_total_bytes", 0) or 0
        
        # If no compression, total_size is the uncompressed size
        if compressed_bytes == 0:
            return total_size
            
        return (total_size - compressed_bytes) + uncompressed_bytes

class ScribeEventsSizeAfterCompressionSensor(ScribeCoordinatorSensor):
    """Sensor for Events Table size (After Compression)."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:database"

    @property
    def native_value(self):
        return self.coordinator.data.get("events_size_bytes", 0)



class ScribeCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for Compression Ratio."""
    
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ratio"

    @property
    def native_value(self):
        data = self.coordinator.data
        uncompressed = data.get("states_uncompressed_total_bytes", 0)
        compressed = data.get("states_compressed_total_bytes", 0)
        
        if not uncompressed or not compressed:
            return None
            
        # Calculate percentage saved
        return round((1 - (compressed / uncompressed)) * 100, 1)


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
        
        Acquires the lock to ensure thread-safe access to the queue length.
        """
        with self._writer._lock:
            return len(self._writer._queue)

class ScribeWriteDurationSensor(ScribeSensor):
    """Sensor for last write duration."""

    def __init__(self, writer, entry):
        self.entity_description = SensorEntityDescription(
            key="write_duration",
            name="Last Write Duration",
            icon="mdi:timer-sand",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="s",
        )
        super().__init__(writer, entry)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._last_write_duration
