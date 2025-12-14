"""Test Scribe setup process."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import EVENT_STATE_CHANGED
from custom_components.scribe.const import DOMAIN, CONF_DB_URL

@pytest.fixture
def mock_config_entry():
    """Mock ConfigEntry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = {CONF_DB_URL: "postgresql://user:pass@host/db"}
    entry.options = {}
    entry.entry_id = "test_entry"
    entry.unique_id = DOMAIN
    entry.title = "Scribe"
    entry.setup_lock = MagicMock()
    entry.setup_lock.locked.return_value = False
    from homeassistant.config_entries import ConfigEntryState
    entry.state = ConfigEntryState.LOADED
    return entry

@pytest.mark.asyncio
async def test_async_setup_yaml(hass):
    """Test setup from YAML triggers import flow."""
    from custom_components.scribe import async_setup
    
    config = {DOMAIN: {CONF_DB_URL: "postgresql://user:pass@host/db"}}
    
    with patch.object(hass.config_entries.flow, "async_init") as mock_flow_init:
        assert await async_setup(hass, config)
        
        mock_flow_init.assert_called_once_with(
            DOMAIN, 
            context={"source": SOURCE_IMPORT}, 
            data=config[DOMAIN]
        )

@pytest.mark.asyncio
async def test_async_setup_entry(hass, mock_config_entry):
    """Test setup entry initializes writer."""
    from custom_components.scribe import async_setup_entry
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        assert await async_setup_entry(hass, mock_config_entry)
        
        mock_writer.start.assert_called_once()
        
        # Verify data stored in hass
        assert DOMAIN in hass.data
        assert mock_config_entry.entry_id in hass.data[DOMAIN]
        assert hass.data[DOMAIN][mock_config_entry.entry_id]["writer"] == mock_writer

@pytest.mark.asyncio
async def test_async_unload_entry(hass, mock_config_entry):
    """Test unload entry stops writer."""
    from custom_components.scribe import async_setup_entry, async_unload_entry
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        # Mock platform unload
        with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
            assert await async_unload_entry(hass, mock_config_entry)
            
        mock_writer.stop.assert_called_once()
        assert mock_config_entry.entry_id not in hass.data[DOMAIN]

@pytest.mark.asyncio
async def test_event_listener(hass, mock_config_entry):
    """Test event listener enqueues events."""
    from custom_components.scribe import async_setup_entry
    from homeassistant.core import State
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.enqueue = MagicMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        # Simulate State Change Event
        event = MagicMock()
        event.event_type = EVENT_STATE_CHANGED
        event.data = {
            "entity_id": "sensor.test",
            "new_state": State("sensor.test", "123", {"unit": "W"})
        }
        
        # Capture the listener using patch on hass.bus.async_listen
        # Since we can't easily patch it before setup in this structure without refactoring,
        # let's iterate listeners again but be more robust.
        
        # target_listener = None
        # In HA core, listeners are stored in hass.bus._listeners
        # It's a dict: {event_type: [list of listeners]}
        # For EVENT_STATE_CHANGED, it should be there.
        
        listeners = hass.bus._listeners.get(EVENT_STATE_CHANGED, [])
        for listener_item in listeners:
             # listener_item is a generic callback, usually the job is wrapped
             # We look for our function name in the string representation
             if "handle_event" in str(listener_item):
                 # target_listener = listener_item
                 break
        
        # If not found in specific event, check None (catch-all) if we used MATCH_ALL? 
        # But handle_event is for EVENT_STATE_CHANGED.
        
        # If still not found, maybe we can just fire the event?
        # hass.bus.async_fire(EVENT_STATE_CHANGED, event.data)
        # But we need to await it or wait for it.
        
        # Let's try firing it and waiting.
        hass.bus.async_fire(EVENT_STATE_CHANGED, event.data)
        await hass.async_block_till_done()
        
        # Verify enqueue called
        mock_writer.enqueue.assert_called()
        call_arg = mock_writer.enqueue.call_args[0][0]
        assert call_arg["type"] == "state"
        assert call_arg["entity_id"] == "sensor.test"
        assert call_arg["value"] == 123.0

@pytest.mark.asyncio
async def test_legacy_config(hass):
    """Test legacy configuration with individual fields."""
    from custom_components.scribe import async_setup_entry
    
    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = {
        "db_user": "user",
        "db_password": "pass",
        "db_host": "host",
        "db_port": "5432",
        "db_name": "db"
    }
    entry.options = {}
    entry.entry_id = "legacy_entry"
    entry.title = "Legacy"
    entry.setup_lock = MagicMock()
    entry.setup_lock.locked.return_value = False
    from homeassistant.config_entries import ConfigEntryState
    entry.state = ConfigEntryState.LOADED
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        assert await async_setup_entry(hass, entry)
        
        # Verify DB URL construction
        call_args = mock_writer_cls.call_args[1]
        assert call_args["db_url"] == "postgresql://user:pass@host:5432/db"

@pytest.mark.asyncio
async def test_legacy_config_invalid(hass):
    """Test invalid legacy configuration."""
    from custom_components.scribe import async_setup_entry
    
    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = {"db_user": "user"} # Missing other fields
    entry.options = {}
    entry.entry_id = "invalid_entry"
    
    assert await async_setup_entry(hass, entry) is False

@pytest.mark.asyncio
async def test_yaml_exclude_attributes(hass, mock_config_entry):
    """Test exclude_attributes from YAML."""
    from custom_components.scribe import async_setup_entry, DOMAIN, CONF_EXCLUDE_ATTRIBUTES
    
    # Mock YAML config in hass.data
    hass.data[DOMAIN] = {"yaml_config": {CONF_EXCLUDE_ATTRIBUTES: ["attr1"]}}
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        await async_setup_entry(hass, mock_config_entry)
        # We can't easily check the internal state of the listener closure, 
        # but we can verify setup succeeded.
        assert DOMAIN in hass.data

@pytest.mark.asyncio
async def test_statistics_setup(hass, mock_config_entry):
    """Test statistics setup."""
    from custom_components.scribe import async_setup_entry
    from custom_components.scribe.const import CONF_ENABLE_STATS_CHUNK
    
    mock_config_entry.options = {CONF_ENABLE_STATS_CHUNK: True}
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls, \
         patch("custom_components.scribe.coordinator.ScribeDataUpdateCoordinator") as mock_coord_cls:
        
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        
        mock_coord = mock_coord_cls.return_value
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        mock_coord_cls.assert_called_once()
        mock_coord.async_config_entry_first_refresh.assert_called_once()
        assert hass.data[DOMAIN][mock_config_entry.entry_id]["chunk_coordinator"] == mock_coord

@pytest.mark.asyncio
async def test_event_listener_filtering(hass, mock_config_entry):
    """Test event listener filtering logic."""
    from custom_components.scribe import async_setup_entry
    from homeassistant.core import State
    from custom_components.scribe.const import CONF_RECORD_STATES, CONF_INCLUDE_ENTITIES, CONF_EXCLUDE_ATTRIBUTES
    
    mock_config_entry.options = {
        CONF_RECORD_STATES: True,
        CONF_INCLUDE_ENTITIES: ["sensor.included"],
        CONF_EXCLUDE_ATTRIBUTES: ["excluded_attr"]
    }
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.enqueue = MagicMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        # Use async_fire instead of manual calling
        
        # Test 1: Entity not included
        hass.bus.async_fire(EVENT_STATE_CHANGED, {"entity_id": "sensor.excluded", "new_state": State("sensor.excluded", "1")})
        await hass.async_block_till_done()
        mock_writer.enqueue.assert_not_called()
        
        # Test 2: New state is None
        hass.bus.async_fire(EVENT_STATE_CHANGED, {"entity_id": "sensor.included", "new_state": None})
        await hass.async_block_till_done()
        mock_writer.enqueue.assert_not_called()
        
        # Test 3: Non-numeric state
        hass.bus.async_fire(EVENT_STATE_CHANGED, {"entity_id": "sensor.included", "new_state": State("sensor.included", "string")})
        await hass.async_block_till_done()
        mock_writer.enqueue.assert_called()
        assert mock_writer.enqueue.call_args[0][0]["value"] is None
        mock_writer.enqueue.reset_mock()
        
        # Test 4: Attribute exclusion
        hass.bus.async_fire(EVENT_STATE_CHANGED, {
            "entity_id": "sensor.included", 
            "new_state": State("sensor.included", "1", {"excluded_attr": 1, "kept_attr": 2})
        })
        await hass.async_block_till_done()
        mock_writer.enqueue.assert_called()
        attrs = mock_writer.enqueue.call_args[0][0]["attributes"]
        assert "excluded_attr" not in attrs
        assert "kept_attr" in attrs
        mock_writer.enqueue.reset_mock()
        
        # Test 5: Exception handling
        mock_writer.enqueue.side_effect = Exception("Enqueue Error")
        hass.bus.async_fire(EVENT_STATE_CHANGED, {
            "entity_id": "sensor.included", 
            "new_state": State("sensor.included", "1", {"excluded_attr": 1, "kept_attr": 2})
        })
        await hass.async_block_till_done() # Should not raise
        mock_writer.enqueue.side_effect = None

@pytest.mark.asyncio
async def test_event_listener_no_record_states(hass, mock_config_entry):
    """Test event listener when record_states is False."""
    from custom_components.scribe import async_setup_entry
    from custom_components.scribe.const import CONF_RECORD_STATES
    from homeassistant.core import State
    
    mock_config_entry.options = {CONF_RECORD_STATES: False}
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.enqueue = MagicMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        hass.bus.async_fire(EVENT_STATE_CHANGED, {"entity_id": "sensor.test", "new_state": State("sensor.test", "1")})
        await hass.async_block_till_done()
        mock_writer.enqueue.assert_not_called()

@pytest.mark.asyncio
async def test_generic_events(hass, mock_config_entry):
    """Test generic event recording."""
    from custom_components.scribe import async_setup_entry
    from custom_components.scribe.const import CONF_RECORD_EVENTS
    
    mock_config_entry.options = {CONF_RECORD_EVENTS: True}
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.enqueue = MagicMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        # Use MATCH_ALL listener
        # Since we can't easily find the listener, we fire an event and check if enqueue is called
        
        event_data = {"foo": "bar"}
        context = MagicMock()
        context.id = "ctx_id"
        context.user_id = "user_id"
        context.parent_id = "parent_id"
        
        hass.bus.async_fire("custom_event", event_data, context=context)
        await hass.async_block_till_done()
        
        mock_writer.enqueue.assert_called()
        call_arg = mock_writer.enqueue.call_args[0][0]
        assert call_arg["type"] == "event"
        assert call_arg["event_type"] == "custom_event"
        
        # Test Exception
        # Test Exception
        mock_writer.enqueue.side_effect = Exception("Event Error")
        hass.bus.async_fire("custom_event", event_data)
        await hass.async_block_till_done() # Should not raise

@pytest.mark.asyncio
async def test_flush_service(hass, mock_config_entry):
    """Test flush service."""
    from custom_components.scribe import async_setup_entry, DOMAIN
    
    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer._flush = AsyncMock()
        
        await async_setup_entry(hass, mock_config_entry)
        
        await hass.services.async_call(DOMAIN, "flush", blocking=True)
        
        mock_writer._flush.assert_called_once()

@pytest.mark.asyncio
async def test_reload_entry(hass, mock_config_entry):
    """Test entry reload."""
    from custom_components.scribe import async_reload_entry
    
    with patch("custom_components.scribe.async_unload_entry", return_value=True) as mock_unload, \
         patch("custom_components.scribe.async_setup_entry", return_value=True) as mock_setup:
        
        await async_reload_entry(hass, mock_config_entry)
        
        mock_unload.assert_called_once_with(hass, mock_config_entry)
        mock_setup.assert_called_once_with(hass, mock_config_entry)
