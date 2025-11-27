"""Test metadata sync logic."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from homeassistant.core import HomeAssistant
from custom_components.scribe import async_setup_entry

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

@pytest.mark.asyncio
async def test_realtime_metadata_sync(hass: HomeAssistant, mock_writer, mock_registries):
    """Test real-time metadata synchronization."""
    mock_er, mock_ar, mock_dr = mock_registries
    
    # Setup entry
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
    mock_config_entry.state = MagicMock()
    mock_config_entry.state.value = "loaded"
    mock_config_entry.async_unload = AsyncMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries.return_value = [mock_config_entry]
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    # Mock ScribeWriter constructor
    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_er), \
         patch("homeassistant.helpers.area_registry.async_get", return_value=mock_ar), \
         patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dr), \
         patch("homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock) as mock_get_users:
        
        mock_get_users.return_value = []
        
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

        # Reset mocks to clear startup calls
        mock_writer.write_entities.reset_mock()
        mock_writer.write_devices.reset_mock()
        mock_writer.write_areas.reset_mock()

        # Test Entity Update
        # Update mock registry first
        new_entity = MagicMock()
        new_entity.entity_id = "light.new"
        new_entity.unique_id = "new_unique_id"
        new_entity.platform = "mqtt"
        new_entity.domain = "light"
        new_entity.name = "New Light"
        new_entity.device_id = None
        new_entity.area_id = None
        new_entity.capabilities = None
        mock_er.async_get.return_value = new_entity

        hass.bus.async_fire("entity_registry_updated", {"action": "create", "entity_id": "light.new"})
        await hass.async_block_till_done()

        assert mock_writer.write_entities.called
        args = mock_writer.write_entities.call_args[0][0]
        assert args[0]["entity_id"] == "light.new"
        assert args[0]["name"] == "New Light"

        # Test Device Update
        new_device = MagicMock()
        new_device.id = "device_new"
        new_device.name = "New Device"
        new_device.name_by_user = "My New Device"
        new_device.model = "v2"
        new_device.manufacturer = "Acme"
        new_device.sw_version = "2.0"
        new_device.area_id = "area_1"
        new_device.config_entries = {"entry_1"}
        mock_dr.async_get.return_value = new_device

        hass.bus.async_fire("device_registry_updated", {"action": "update", "device_id": "device_new"})
        await hass.async_block_till_done()

        assert mock_writer.write_devices.called
        args = mock_writer.write_devices.call_args[0][0]
        assert args[0]["device_id"] == "device_new"
        assert args[0]["name"] == "New Device"

        # Test Area Update
        new_area = MagicMock()
        new_area.id = "area_new"
        new_area.name = "New Area"
        new_area.picture = None
        mock_ar.async_get_area.return_value = new_area

        hass.bus.async_fire("area_registry_updated", {"action": "create", "area_id": "area_new"})
        await hass.async_block_till_done()

        assert mock_writer.write_areas.called
        args = mock_writer.write_areas.call_args[0][0]
        assert args[0]["area_id"] == "area_new"
        assert args[0]["name"] == "New Area"

        # Test User Update
        new_user = MagicMock()
        new_user.id = "user_new"
        new_user.name = "New User"
        new_user.is_owner = False
        new_user.is_active = True
        new_user.system_generated = False
        new_user.groups = []
        
        # Mock async_get_user
        hass.auth.async_get_user = AsyncMock(return_value=new_user)

        hass.bus.async_fire("user_added", {"user_id": "user_new"})
        await hass.async_block_till_done()

        assert mock_writer.write_users.called
        args = mock_writer.write_users.call_args[0][0]
        assert args[0]["user_id"] == "user_new"
        assert args[0]["name"] == "New User"
