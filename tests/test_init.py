"""Test Scribe setup process."""
from unittest.mock import patch, MagicMock
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
        mock_writer.init_db = MagicMock()
        mock_writer.start = MagicMock()
        mock_writer.shutdown = MagicMock()

        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        assert len(mock_writer_cls.mock_calls) > 0
        mock_writer.start.assert_called_once()

        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_writer.shutdown.assert_called()
