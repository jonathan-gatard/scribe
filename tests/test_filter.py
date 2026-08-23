"""Test Scribe filtering logic."""

from unittest.mock import Mock, patch, AsyncMock
import pytest

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.scribe.const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_INCLUDE_ENTITY_GLOBS,
)


@pytest.fixture(autouse=True)
def accept_any_database():
    """A first YAML setup now validates the database before creating the entry.

    These tests are about filtering and never reach a real database, so the
    check is answered directly instead of opening a socket.
    """
    with patch(
        "custom_components.scribe.config_flow._check_database",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


@pytest.fixture
def mock_writer():
    """Mock the Scribe writer instance."""
    # Create a mock instance
    mock_instance = Mock()

    # Make async methods awaitable
    mock_instance.start = AsyncMock()
    mock_instance.stop = AsyncMock()
    mock_instance.write_users = AsyncMock()
    mock_instance.write_entities = AsyncMock()
    mock_instance.write_devices = AsyncMock()
    mock_instance.write_areas = AsyncMock()
    mock_instance.write_integrations = AsyncMock()
    mock_instance.query = AsyncMock()
    mock_instance._flush = AsyncMock()

    # enqueue is synchronous
    mock_instance.enqueue = Mock()

    # Attributes
    mock_instance.enable_table_users = True
    mock_instance.enable_table_devices = True
    mock_instance.enable_table_areas = True
    mock_instance.enable_table_integrations = True

    return mock_instance


async def test_exclude_entity_globs(hass: HomeAssistant, mock_writer):
    """Test excluding entities by glob."""
    config = {
        DOMAIN: {
            CONF_DB_URL: "postgresql://user:pass@localhost/db",
            CONF_EXCLUDE_ENTITY_GLOBS: ["sensor.exclude_*", "switch.*_exclude"],
        }
    }

    # Patch the ScribeWriter class to return our mock instance
    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer):
        assert await async_setup_component(hass, DOMAIN, config)
        await hass.async_block_till_done()

    # Verify writer was started
    assert mock_writer.start.called

    # Get writer from hass.data - should be our mock
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]

    # Note: async_setup_component triggers config entry setup, which calls async_setup_entry
    # We verify that our writer mock is indeed in hass.data
    writer = hass.data[DOMAIN][entry.entry_id]["writer"]
    assert writer == mock_writer

    # Test valid entity (should NOT be filtered)
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.valid_entity",
            "new_state": Mock(state="10", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert writer.enqueue.call_count == 1
    writer.enqueue.reset_mock()

    # Test excluded entity 1 (sensor.exclude_*)
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.exclude_me",
            "new_state": Mock(state="10", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert writer.enqueue.call_count == 0

    # Test excluded entity 2 (switch.*_exclude)
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "switch.bedroom_exclude",
            "new_state": Mock(state="on", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert writer.enqueue.call_count == 0


async def test_include_entity_globs(hass: HomeAssistant, mock_writer):
    """Test including entities by glob (whitelist mode)."""
    # Whitelist mode: only matching entities should be recorded
    config = {
        DOMAIN: {
            CONF_DB_URL: "postgresql://user:pass@localhost/db",
            CONF_INCLUDE_ENTITY_GLOBS: ["sensor.include_*"],
        }
    }

    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer):
        assert await async_setup_component(hass, DOMAIN, config)
        await hass.async_block_till_done()

    # Test included entity
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.include_me",
            "new_state": Mock(state="10", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert mock_writer.enqueue.call_count == 1
    mock_writer.enqueue.reset_mock()

    # Test non-included entity
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.other_entity",
            "new_state": Mock(state="10", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert mock_writer.enqueue.call_count == 0


async def test_exclude_entity_globs_overrides_include_entity_globs(
    hass: HomeAssistant, mock_writer
):
    """Regression for jonathan-gtd/scribe#33.

    Home Assistant's ``generate_filter`` lets ``include_entity_globs`` short-
    circuit over ``exclude_entity_globs`` (case 4a). Scribe wraps the
    upstream filter so an exclude-glob match is always a hard reject —
    matching the user-visible expectation that excludes win over includes.
    """
    config = {
        DOMAIN: {
            CONF_DB_URL: "postgresql://user:pass@localhost/db",
            CONF_INCLUDE_ENTITY_GLOBS: ["sensor.*_temperature"],
            CONF_EXCLUDE_ENTITY_GLOBS: ["sensor.processor_*"],
        }
    }

    with patch("custom_components.scribe.ScribeWriter", return_value=mock_writer):
        assert await async_setup_component(hass, DOMAIN, config)
        await hass.async_block_till_done()

    # Matches include_entity_globs only — should be recorded.
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.living_room_temperature",
            "new_state": Mock(state="22", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert mock_writer.enqueue.call_count == 1
    mock_writer.enqueue.reset_mock()

    # Matches BOTH include_entity_globs (sensor.*_temperature) AND
    # exclude_entity_globs (sensor.processor_*). Pre-fix, the upstream
    # filter recorded this entity because the include glob short-
    # circuited. Post-fix, the exclude glob wins.
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.processor_temperature",
            "new_state": Mock(state="62", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert mock_writer.enqueue.call_count == 0, (
        "sensor.processor_temperature must be excluded — exclude_entity_globs "
        "should override include_entity_globs"
    )

    # Matches exclude_entity_globs only — must remain excluded.
    hass.bus.async_fire(
        EVENT_STATE_CHANGED,
        {
            "entity_id": "sensor.processor_use",
            "new_state": Mock(state="42", attributes={}),
        },
    )
    await hass.async_block_till_done()
    assert mock_writer.enqueue.call_count == 0
