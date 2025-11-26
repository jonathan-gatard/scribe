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
            ScribeDatabaseSizeSensor(coordinator, entry, "db_size", "Database Size"),
            ScribeCompressionRatioSensor(coordinator, entry, "states_compression", "States Compression Ratio"),
            ScribeCompressedSizeSensor(coordinator, entry, "states_compressed_bytes", "States Compressed Size"),
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

class ScribeDatabaseSizeSensor(ScribeCoordinatorSensor):
    """Sensor for DB size."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:database"

    @property
    def native_value(self):
        states_size = self.coordinator.data.get("states_size_bytes", 0) or 0
        events_size = self.coordinator.data.get("events_size_bytes", 0) or 0
        return states_size + events_size

class ScribeCompressedSizeSensor(ScribeCoordinatorSensor):
    """Sensor for Compressed DB size."""
    
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = "data_size"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:zip-box"

    @property
    def native_value(self):
        return self.coordinator.data.get(self._key)

class ScribeCompressionRatioSensor(ScribeCoordinatorSensor):
    """Sensor for Compression Ratio."""
    
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ratio"

    @property
    def native_value(self):
        data = self.coordinator.data
        uncompressed = data.get("states_uncompressed_bytes", 0)
        compressed = data.get("states_compressed_bytes", 0)
        
        if not uncompressed or not compressed:
            return None
            
        # Calculate percentage saved
        return round((1 - (compressed / uncompressed)) * 100, 1)


class ScribeStatesWrittenSensor(ScribeSensor):
    """Sensor for total states written."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = SensorEntityDescription(
            key="states_written",
            name="States Written",
            icon="mdi:database-plus",
            state_class=SensorStateClass.TOTAL_INCREASING,
        )

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._states_written

class ScribeEventsWrittenSensor(ScribeSensor):
    """Sensor for total events written."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = SensorEntityDescription(
            key="events_written",
            name="Events Written",
            icon="mdi:database-plus",
            state_class=SensorStateClass.TOTAL_INCREASING,
        )

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._events_written

class ScribeBufferSizeSensor(ScribeSensor):
    """Sensor for current buffer size."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = SensorEntityDescription(
            key="buffer_size",
            name="Buffer Size",
            icon="mdi:buffer",
            state_class=SensorStateClass.MEASUREMENT,
        )

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
        super().__init__(writer, entry)
        self.entity_description = SensorEntityDescription(
            key="write_duration",
            name="Last Write Duration",
            icon="mdi:timer-sand",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="s",
        )

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._last_write_duration
