"""System health support for Scribe."""
from __future__ import annotations

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)

async def system_health_info(hass: HomeAssistant) -> dict[str, str]:
    """Get info for the info page."""
    return {
        "version": hass.data[DOMAIN].get("version", "Unknown"),
        "connected": "Yes" if hass.data.get(DOMAIN) else "No",
    }
