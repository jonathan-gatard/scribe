"""Test configuration options for metadata tables."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from homeassistant.config_entries import ConfigEntry
from custom_components.scribe.const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_ENABLE_AREAS,
    CONF_ENABLE_DEVICES,
    CONF_ENABLE_INTEGRATIONS,
    CONF_ENABLE_USERS,
)
from custom_components.scribe import async_setup_entry


@pytest.fixture
def mock_config_entry():
    """Mock ConfigEntry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = {CONF_DB_URL: "postgresql://user:pass@host/db"}
    entry.options = {}
    entry.entry_id = "test_entry"
    entry.title = "Scribe"
    entry.setup_lock = MagicMock()
    entry.setup_lock.locked.return_value = False
    from homeassistant.config_entries import ConfigEntryState

    entry.state = ConfigEntryState.LOADED
    return entry


@pytest.mark.asyncio
async def test_default_config_enables_all(hass, mock_config_entry):
    """Test that default config enables all metadata tables."""
    with (
        patch("custom_components.scribe.ScribeWriter") as mock_writer_cls,
        patch(
            "homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock
        ) as mock_get_users,
    ):
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.write_users = AsyncMock()
        mock_writer.write_entities = AsyncMock()
        mock_writer.write_areas = AsyncMock()
        mock_writer.write_devices = AsyncMock()
        mock_writer.write_integrations = AsyncMock()

        # Mock flags on the writer instance (simulating what __init__ does)
        mock_writer.enable_table_users = True
        mock_writer.enable_table_areas = True
        mock_writer.enable_table_devices = True
        mock_writer.enable_table_integrations = True

        mock_get_users.return_value = []  # Return empty list to avoid actual processing but verify call

        await async_setup_entry(hass, mock_config_entry)

        # Verify ScribeWriter init was called with defaults (True)
        # Note: We can't easily check init args of the mock class directly if it's called inside the function
        # But we can check if the sync methods were called (which implies flags were True in __init__.py logic)

        # Since we mocked async_get_users to return empty, write_users won't be called,
        # but we can check if async_get_users was called.
        mock_get_users.assert_called_once()

        # For others, we can check if the registry getters were called, but simpler to check if writer was initialized with correct flags
        _, kwargs = mock_writer_cls.call_args
        assert kwargs["enable_table_users"] is True
        assert kwargs["enable_table_areas"] is True
        assert kwargs["enable_table_devices"] is True
        assert kwargs["enable_table_integrations"] is True


@pytest.mark.asyncio
async def test_disable_users_table(hass, mock_config_entry):
    """Test disabling users table."""
    # Setup hass.data for YAML config
    hass.data[DOMAIN] = {"yaml_config": {CONF_ENABLE_USERS: False}}

    with (
        patch("custom_components.scribe.ScribeWriter") as mock_writer_cls,
        patch(
            "homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock
        ) as mock_get_users,
    ):
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()

        # Mock flags on the writer instance
        mock_writer.enable_table_users = False

        await async_setup_entry(hass, mock_config_entry)

        # Verify ScribeWriter init
        _, kwargs = mock_writer_cls.call_args
        assert kwargs["enable_table_users"] is False

        # Verify async_get_users was NOT called
        mock_get_users.assert_not_called()


@pytest.mark.asyncio
async def test_disable_all_metadata(hass, mock_config_entry):
    """Test disabling all metadata tables."""
    # Setup hass.data for YAML config
    hass.data[DOMAIN] = {
        "yaml_config": {
            CONF_ENABLE_USERS: False,
            CONF_ENABLE_AREAS: False,
            CONF_ENABLE_DEVICES: False,
            CONF_ENABLE_INTEGRATIONS: False,
        }
    }

    with (
        patch("custom_components.scribe.ScribeWriter") as mock_writer_cls,
        patch(
            "homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock
        ) as mock_get_users,
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch("homeassistant.helpers.area_registry.async_get") as mock_ar,
        patch("homeassistant.helpers.device_registry.async_get") as mock_dr,
    ):
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()

        # Mock flags on the writer instance
        mock_writer.enable_table_users = False
        mock_writer.enable_table_areas = False
        mock_writer.enable_table_devices = False
        mock_writer.enable_table_integrations = False

        await async_setup_entry(hass, mock_config_entry)

        # Verify ScribeWriter init
        _, kwargs = mock_writer_cls.call_args
        assert kwargs["enable_table_users"] is False
        assert kwargs["enable_table_areas"] is False
        assert kwargs["enable_table_devices"] is False
        assert kwargs["enable_table_integrations"] is False

        # Verify no registry calls (entities still syncs since the table is mandatory)
        mock_get_users.assert_not_called()
        mock_ar.assert_not_called()
        mock_dr.assert_not_called()
