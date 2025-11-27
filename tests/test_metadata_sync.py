"""Test metadata sync logic."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from homeassistant.core import HomeAssistant
from custom_components.scribe import async_setup_entry
from custom_components.scribe.const import DOMAIN

@pytest.fixture
def mock_writer():
    """Mock the ScribeWriter."""
    writer = MagicMock()
    writer.start = AsyncMock()
    writer.stop = AsyncMock()
    writer.write_users = AsyncMock()
    writer.write_entities = AsyncMock()
    writer.write_areas = AsyncMock()
    writer.write_devices = AsyncMock()
    writer.write_integrations = AsyncMock()
    return writer

@pytest.fixture
def mock_registries():
    """Mock the registries."""
    # Entity Registry
    er = MagicMock()
    er.entities = {}

    # Area Registry
    ar = MagicMock()
    area1 = MagicMock()
    area1.id = "area_1"
    area1.name = "Living Room"
    area1.picture = "living_room.jpg"
    ar.areas = {"area_1": area1}

    # Device Registry
    dr = MagicMock()
    device1 = MagicMock()
    device1.id = "device_1"
    device1.name = "Hue Bulb"
    device1.name_by_user = "Living Room Lamp"
    device1.model = "LCT001"
    device1.manufacturer = "Philips"
    device1.sw_version = "1.0.0"
    device1.area_id = "area_1"
    device1.config_entries = {"entry_1"}
    dr.devices = {"device_1": device1}

    return er, ar, dr

@pytest.mark.asyncio
async def test_metadata_sync(hass: HomeAssistant, mock_writer, mock_registries):
    """Test that metadata is synced on startup."""
    mock_er, mock_ar, mock_dr = mock_registries
    
    # Mock config entry
    entry = MagicMock()
    entry.data = {
        "db_url": "postgresql://user:pass@localhost/db",
        "record_states": True,
        "record_events": True
    }
    entry.options = {}
    entry.entry_id = "test_entry"

    # Mock Config Entries
    mock_config_entry = MagicMock()
    mock_config_entry.entry_id = "entry_1"
    mock_config_entry.domain = "hue"
    mock_config_entry.title = "Philips Hue"
    mock_config_entry.source = "discovery"
    mock_config_entry.async_unload = AsyncMock()
    # Mock state enum
    state_mock = MagicMock()
    state_mock.value = "loaded"
    mock_config_entry.state = state_mock

    hass.config_entries = MagicMock()
    hass.config_entries.async_entries.return_value = [mock_config_entry]
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    # Mock ScribeWriter constructor to return our mock
    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_er), \
         patch("homeassistant.helpers.area_registry.async_get", return_value=mock_ar), \
         patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr), \
         patch("homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock) as mock_get_users:
        
        mock_get_users.return_value = [] # Mock empty users

        # Run setup
        await async_setup_entry(hass, entry)

        # Verify write_areas
        assert mock_writer.write_areas.called
        areas_arg = mock_writer.write_areas.call_args[0][0]
        assert len(areas_arg) == 1
        assert areas_arg[0]["area_id"] == "area_1"
        assert areas_arg[0]["name"] == "Living Room"

        # Verify write_devices
        assert mock_writer.write_devices.called
        devices_arg = mock_writer.write_devices.call_args[0][0]
        assert len(devices_arg) == 1
        assert devices_arg[0]["device_id"] == "device_1"
        assert devices_arg[0]["manufacturer"] == "Philips"
        assert devices_arg[0]["primary_config_entry"] == "entry_1"

        # Verify write_integrations
        assert mock_writer.write_integrations.called
        integrations_arg = mock_writer.write_integrations.call_args[0][0]
        assert len(integrations_arg) == 1
        assert integrations_arg[0]["entry_id"] == "entry_1"
        assert integrations_arg[0]["domain"] == "hue"
        assert integrations_arg[0]["state"] == "loaded"
