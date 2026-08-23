"""System health support for Scribe."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .writer import _safe_target


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


def _writer(hass: HomeAssistant):
    """The running writer, or None when setup never got that far."""
    for value in (hass.data.get(DOMAIN) or {}).values():
        if isinstance(value, dict) and value.get("writer") is not None:
            return value["writer"]
    return None


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page.

    Reports the database, not the integration: "connected" used to be true
    whenever Scribe was loaded at all, so the panel said everything was fine
    while the database was unreachable and nothing was being recorded. The
    version came from a key nothing ever set, so it always read "Unknown".
    """
    integration = await async_get_integration(hass, DOMAIN)
    info: dict[str, Any] = {"version": str(integration.version)}

    writer = _writer(hass)
    if writer is None:
        info["database"] = "not set up"
        return info

    info["database"] = _safe_target(writer.db_url)
    info["connected"] = writer._connected
    info["timescaledb"] = writer._has_timescaledb
    info["buffered_items"] = len(writer._queue)
    return info
