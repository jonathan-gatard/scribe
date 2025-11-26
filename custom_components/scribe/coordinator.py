"""DataUpdateCoordinator for Scribe."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class ScribeDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Scribe data."""

    def __init__(self, hass: HomeAssistant, writer) -> None:
        """Initialize."""
        self.writer = writer
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
        )

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            # Writer is now async, so we await it directly
            return await self.writer.get_db_stats()
        except Exception as err:
            raise UpdateFailed(f"Error communicating with database: {err}")
