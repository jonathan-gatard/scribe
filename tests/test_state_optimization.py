"""Test state optimization logic."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from custom_components.scribe.const import DOMAIN, CONF_DB_URL
from homeassistant.core import State


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
async def test_state_optimization(hass, mock_config_entry):
    """Test that state string is nullified when value is numeric."""
    from custom_components.scribe import async_setup_entry

    with patch("custom_components.scribe.ScribeWriter") as mock_writer_cls:
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.enqueue = MagicMock()

        await async_setup_entry(hass, mock_config_entry)

        # Test 1: Numeric state
        # Expectation: value=123.0, state=None
        hass.bus.async_fire(
            EVENT_STATE_CHANGED,
            {
                "entity_id": "sensor.numeric",
                "new_state": State("sensor.numeric", "123.0"),
            },
        )
        await hass.async_block_till_done()

        mock_writer.enqueue.assert_called()
        call_arg = mock_writer.enqueue.call_args[0][0]
        assert call_arg["entity_id"] == "sensor.numeric"
        assert call_arg["value"] == 123.0
        assert call_arg["state"] is None

        mock_writer.enqueue.reset_mock()

        # Test 2: Non-numeric state
        # Expectation: value=None, state="string"
        hass.bus.async_fire(
            EVENT_STATE_CHANGED,
            {
                "entity_id": "sensor.string",
                "new_state": State("sensor.string", "not_a_number"),
            },
        )
        await hass.async_block_till_done()

        mock_writer.enqueue.assert_called()
        call_arg = mock_writer.enqueue.call_args[0][0]
        assert call_arg["entity_id"] == "sensor.string"
        assert call_arg["value"] is None
        assert call_arg["state"] == "not_a_number"

        mock_writer.enqueue.reset_mock()

        # Test 3: Mixed state (numeric string but not float parsable? No, float("123") works)
        # Test integer string
        hass.bus.async_fire(
            EVENT_STATE_CHANGED,
            {"entity_id": "sensor.int", "new_state": State("sensor.int", "42")},
        )
        await hass.async_block_till_done()

        mock_writer.enqueue.assert_called()
        call_arg = mock_writer.enqueue.call_args[0][0]
        assert call_arg["value"] == 42.0
        assert call_arg["state"] is None
