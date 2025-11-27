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

    def __init__(
        self, 
        hass: HomeAssistant, 
        writer, 
        update_interval_minutes: int = 60,
        stats_type: str = "all"
    ) -> None:
        """Initialize.
        
        Args:
            hass: Home Assistant instance
            writer: ScribeWriter instance
            update_interval_minutes: Update interval in minutes
            stats_type: Type of stats to fetch ("chunk", "size", or "all")
        """
        self.writer = writer
        self.stats_type = stats_type
        
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{stats_type}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            # Writer is now async, so we await it directly
            return await self.writer.get_db_stats(stats_type=self.stats_type)
        except Exception as err:
            raise UpdateFailed(f"Error communicating with database: {err}")
