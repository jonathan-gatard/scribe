"""Test Scribe setup process."""
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.setup import async_setup_component
from custom_components.scribe.const import (
    DOMAIN,
    CONF_DB_HOST,
    CONF_DB_PORT,
    CONF_DB_USER,
    CONF_DB_PASSWORD,
    CONF_DB_NAME,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

async def test_setup_unload_entry(hass):
    """Test setting up and unloading a config entry."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DB_HOST: "localhost",
            CONF_DB_PORT: 5432,
            CONF_DB_USER: "postgres",
            CONF_DB_PASSWORD: "password",
            CONF_DB_NAME: "homeassistant",
        },
        options={}
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.scribe.ScribeWriter"
    ) as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.init_db = AsyncMock()
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()

        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        assert len(mock_writer_cls.mock_calls) > 0
        mock_writer.start.assert_called_once()

        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_writer.stop.assert_called()

@pytest.mark.asyncio
async def test_setup_entry_prevents_duplicates(hass):
    """Test that async_setup_entry aborts if an instance is already running."""
    from custom_components.scribe import async_setup_entry
    
    # Setup first entry
    entry1 = MockConfigEntry(
        domain=DOMAIN,
        data={"db_url": "postgresql://user:pass@host/db"},
        unique_id=DOMAIN,
        entry_id="entry1"
    )
    entry1.add_to_hass(hass)
    
    # Setup second entry
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        data={"db_url": "postgresql://user:pass@host/db"},
        unique_id="other",
        entry_id="entry2"
    )
    entry2.add_to_hass(hass)

    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.init_db = AsyncMock()
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        # First one should succeed
        assert await async_setup_entry(hass, entry1)
        await hass.async_block_till_done()
        
        # Second one should fail (return False)
        assert not await async_setup_entry(hass, entry2)
        await hass.async_block_till_done()
