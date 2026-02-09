"""Test Scribe config flow."""
import pytest
from homeassistant import config_entries, data_entry_flow
from custom_components.scribe.const import DOMAIN, CONF_DB_URL, CONF_RECORD_STATES, CONF_RECORD_EVENTS

@pytest.mark.asyncio
async def test_flow_user_init(hass):
    """Test that the user flow is disabled."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "ui_configuration_disabled"



@pytest.mark.asyncio
async def test_flow_import(hass):
    """Test import from YAML."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT},
        data={
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True
        }
    )
    
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Scribe"

@pytest.mark.asyncio
async def test_flow_abort_if_configured(hass):
    """Test abort if already configured."""
    # Create an existing entry
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scribe",
        data={},
        source=config_entries.SOURCE_USER,
        unique_id=DOMAIN,
        entry_id="existing_entry"
    )
    mock_entry.add_to_hass(hass)

    # Try to create another one via user flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "ui_configuration_disabled"

    # Try to import
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT},
        data={CONF_DB_URL: "postgresql://new/db"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"



@pytest.mark.asyncio
async def test_options_flow(hass, mock_config_entry):
    """Test options flow."""
    from homeassistant.data_entry_flow import FlowResultType
    from custom_components.scribe.const import (
        CONF_BATCH_SIZE, CONF_RECORD_STATES, CONF_RECORD_EVENTS
    )
    
    mock_config_entry.add_to_hass(hass)
    
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    
    user_input = {
        CONF_BATCH_SIZE: 100,
        CONF_RECORD_STATES: False,
        CONF_RECORD_EVENTS: True
    }
    
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BATCH_SIZE] == 100.0
    assert result["data"][CONF_RECORD_STATES] is False
    assert result["data"][CONF_RECORD_EVENTS] is True

@pytest.mark.asyncio
async def test_options_flow_validation_error(hass, mock_config_entry):
    """Test options flow validation error."""
    from homeassistant.data_entry_flow import FlowResultType
    from custom_components.scribe.const import CONF_RECORD_STATES, CONF_RECORD_EVENTS
    
    mock_config_entry.add_to_hass(hass)
    
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    
    user_input = {
        CONF_RECORD_STATES: False,
        CONF_RECORD_EVENTS: False
    }
    
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "must_record_something"}
