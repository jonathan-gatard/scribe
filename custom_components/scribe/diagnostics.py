"""Diagnostics support for Scribe."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DB_URL, CONF_DB_PASSWORD, CONF_DB_USER

TO_REDACT = {CONF_DB_URL, CONF_DB_PASSWORD, CONF_DB_USER}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    `_hass` is unused: Home Assistant calls this platform hook positionally,
    so the parameter has to stay even though everything reported comes from
    the entry itself.
    """
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
    }
