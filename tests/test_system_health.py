"""Test Scribe system health."""

import pytest
from unittest.mock import MagicMock
from custom_components.scribe.system_health import async_register, system_health_info
from custom_components.scribe.const import DOMAIN


@pytest.mark.asyncio
async def test_system_health_register(hass):
    """Test system health registration."""
    register = MagicMock()
    async_register(hass, register)
    register.async_register_info.assert_called_with(system_health_info)


@pytest.mark.asyncio
async def test_system_health_info(hass):
    """Test system health info."""
    hass.data[DOMAIN] = {"version": "1.0.0"}

    info = await system_health_info(hass)
    assert info["version"] == "1.0.0"
    assert info["connected"] == "Yes"

    hass.data[DOMAIN] = {}
    info = await system_health_info(hass)
    assert info["connected"] == "No"
