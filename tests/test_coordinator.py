"""Test Scribe coordinator."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components.scribe.coordinator import ScribeDataUpdateCoordinator


@pytest.mark.asyncio
async def test_coordinator_update_success(hass):
    """Test successful update."""
    writer = MagicMock()
    writer.get_db_stats = AsyncMock(return_value={"size": 100})

    coordinator = ScribeDataUpdateCoordinator(hass, writer)
    data = await coordinator._async_update_data()

    assert data == {"size": 100}


@pytest.mark.asyncio
async def test_coordinator_update_failure(hass):
    """Test failed update."""
    writer = MagicMock()
    writer.get_db_stats = AsyncMock(side_effect=Exception("DB Error"))

    coordinator = ScribeDataUpdateCoordinator(hass, writer)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
