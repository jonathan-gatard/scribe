"""Sensor platform for Scribe."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Scribe sensors."""
    writer = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        ScribeEventsWrittenSensor(writer, entry),
        ScribeBufferSizeSensor(writer, entry),
        ScribeWriteDurationSensor(writer, entry),
    ]
    
    async_add_entities(entities, True)

class ScribeSensor(SensorEntity):
    """Base class for Scribe sensors."""

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

class ScribeEventsWrittenSensor(ScribeSensor):
    """Sensor for total events written."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = type("EntityDescription", (), {
            "key": "events_written",
            "name": "Events Written",
            "icon": "mdi:database-plus",
            "state_class": SensorStateClass.TOTAL_INCREASING,
        })

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._events_written

class ScribeBufferSizeSensor(ScribeSensor):
    """Sensor for current buffer size."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = type("EntityDescription", (), {
            "key": "buffer_size",
            "name": "Buffer Size",
            "icon": "mdi:buffer",
            "state_class": SensorStateClass.MEASUREMENT,
        })

    @property
    def native_value(self):
        """Return the state of the sensor."""
        with self._writer._lock:
            return len(self._writer._queue)

class ScribeWriteDurationSensor(ScribeSensor):
    """Sensor for last write duration."""

    def __init__(self, writer, entry):
        super().__init__(writer, entry)
        self.entity_description = type("EntityDescription", (), {
            "key": "write_duration",
            "name": "Last Write Duration",
            "icon": "mdi:timer-sand",
            "state_class": SensorStateClass.MEASUREMENT,
            "native_unit_of_measurement": "s",
        })

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._writer._last_write_duration
