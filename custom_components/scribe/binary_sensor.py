"""Binary sensor platform for Scribe.

This module provides a binary sensor to indicate the connectivity status of the
database writer. It helps users quickly identify if Scribe is successfully
connected to the TimescaleDB instance.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import re

from .const import DOMAIN

# A state attribute is readable by every Home Assistant user and is written to
# the recorder, the logbook and the history. `last_error` is raw driver text,
# so any connection string inside it would be persisted in all three. Today's
# asyncpg messages do not carry one; this makes sure a future one cannot.
_DSN_PATTERN = re.compile(r"\b[a-z+]*(?:postgres|postgresql)://[^\s'\"]+", re.I)


def _redact_dsn(message: str | None) -> str | None:
    """Replace any connection string in an error message with a marker."""
    if not message:
        return message
    return _DSN_PATTERN.sub("<redacted-database-url>", message)


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

    async_add_entities(entities)


class ScribeConnectionBinarySensor(BinarySensorEntity):
    """Binary sensor for DB connection status.

    Reflects the `_connected` state of the writer.
    Also exposes the last error message as an attribute for debugging.
    """

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
            "manufacturer": "Scribe",
        }
        self.entity_description = BinarySensorEntityDescription(
            key="connected",
            name="Database Connection",
        )

    @property
    def is_on(self) -> bool:
        """Return True if connected."""
        return self._writer._connected

    @property
    def extra_state_attributes(self):
        """Return error message if any, with any connection string removed."""
        return {"last_error": _redact_dsn(self._writer._last_error)}
