"""Test entities sync logic."""
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
    return writer

@pytest.fixture
def mock_entity_registry():
    """Mock the entity registry."""
    registry = MagicMock()
    
    # Create mock entities
    entity1 = MagicMock()
    entity1.entity_id = "light.living_room"
    entity1.unique_id = "unique_id_1"
    entity1.platform = "hue"
    entity1.domain = "light"
    entity1.name = "Living Room Light"
    entity1.original_name = "Hue Light 1"
    entity1.device_id = "device_1"
    entity1.area_id = "area_1"
    entity1.capabilities = {"brightness": True}

    entity2 = MagicMock()
    entity2.entity_id = "sensor.temperature"
    entity2.unique_id = "unique_id_2"
    entity2.platform = "mqtt"
    entity2.domain = "sensor"
    entity2.name = None
    entity2.original_name = "Temperature Sensor"
    entity2.device_id = "device_2"
    entity2.area_id = None
    entity2.capabilities = None

    registry.entities = {
        "light.living_room": entity1,
        "sensor.temperature": entity2
    }
    return registry

@pytest.mark.asyncio
async def test_entities_sync(hass: HomeAssistant, mock_writer, mock_entity_registry):
    """Test that entities are synced on startup."""
    
    # Mock config entry
    entry = MagicMock()
    entry.data = {
        "db_url": "postgresql://user:pass@localhost/db",
        "record_states": True,
        "record_events": True
    }
    entry.options = {}
    entry.entry_id = "test_entry"

    # Mock ScribeWriter constructor to return our mock
    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_entity_registry), \
         patch("homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock) as mock_get_users, \
         patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", new_callable=AsyncMock):
        
        mock_get_users.return_value = [] # Mock empty users to focus on entities

        # Run setup
        await async_setup_entry(hass, entry)

        # Verify write_entities was called
        assert mock_writer.write_entities.called
        
        # Check the data passed to write_entities
        call_args = mock_writer.write_entities.call_args[0][0]
        assert len(call_args) == 2
        
        # Verify first entity
        ent1 = next(e for e in call_args if e["entity_id"] == "light.living_room")
        assert ent1["unique_id"] == "unique_id_1"
        assert ent1["platform"] == "hue"
        assert ent1["name"] == "Living Room Light"
        assert ent1["capabilities"] == '{"brightness": true}'

        # Verify second entity (fallback to original_name)
        ent2 = next(e for e in call_args if e["entity_id"] == "sensor.temperature")
        assert ent2["unique_id"] == "unique_id_2"
        assert ent2["name"] == "Temperature Sensor"
        assert ent2["capabilities"] is None
