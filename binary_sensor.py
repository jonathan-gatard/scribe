"""Binary sensor platform for Scribe."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Scribe binary sensors."""
    writer = hass.data[DOMAIN][entry.entry_id]["writer"]
    
    entities = [
        ScribeConnectionBinarySensor(writer, entry),
    ]
    
    async_add_entities(entities, True)

class ScribeConnectionBinarySensor(BinarySensorEntity):
    """Binary sensor for DB connection status."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, writer, entry):
        """Initialize the sensor."""
        self._writer = writer
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Scribe",
            "manufacturer": "Jonathan Gatard",
        }
        self.entity_description = type("EntityDescription", (), {
            "key": "connected",
            "name": "Database Connection",
        })

    @property
    def is_on(self) -> bool:
        """Return True if connected."""
        return self._writer._connected

    @property
    def extra_state_attributes(self):
        """Return error message if any."""
        return {
            "last_error": self._writer._last_error
        }
