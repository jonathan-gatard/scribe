"""Test ScribeWriter device writing."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from custom_components.scribe.writer import ScribeWriter


@pytest.fixture
def writer(hass):
    """Create a writer instance with a mock pool."""
    mock_pool = MagicMock()
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=10,
        flush_interval=5,
        max_queue_size=100,
        buffer_on_failure=False,
        table_name_states="states",
        table_name_events="events",
    )
    # Inject mock pool
    writer._pool = mock_pool
    # Mock _execute_many
    writer._execute_many = AsyncMock()
    return writer


@pytest.mark.asyncio
async def test_write_devices_converts_int_to_str(writer):
    """Test that integer sw_version is converted to string."""
    devices = [
        {
            "device_id": "test_device",
            "name": "Test Device",
            "name_by_user": None,
            "model": 12345,  # Integer model
            "manufacturer": 67890,  # Integer manufacturer
            "sw_version": 20211110,  # Integer version
            "area_id": "test_area",
            "primary_config_entry": "test_entry",
        }
    ]

    await writer.write_devices(devices)

    # Verify that _execute_many was called
    assert writer._execute_many.called

    # Get the arguments passed to _execute_many
    # call_args[0][1] is the list of parameters (rows)
    call_args = writer._execute_many.call_args
    params = call_args[0][1]  # This is the 'rows' list

    assert len(params) == 1
    # row is (device_id, name, name_by_user, model, manufacturer, sw_version, area_id, primary_config_entry)
    row = params[0]

    # sw_version is index 5
    assert isinstance(row[5], str)
    assert row[5] == "20211110"

    # model is index 3
    assert isinstance(row[3], str)
    assert row[3] == "12345"

    # manufacturer is index 4
    assert isinstance(row[4], str)
    assert row[4] == "67890"
