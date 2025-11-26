"""Test Scribe config flow."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from homeassistant import config_entries, data_entry_flow
from custom_components.scribe.const import DOMAIN, CONF_DB_URL, CONF_RECORD_STATES, CONF_RECORD_EVENTS

@pytest.mark.asyncio
async def test_flow_user_init(hass):
    """Test the initialization of the user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

@pytest.mark.asyncio
async def test_flow_user_valid_input(hass, mock_create_async_engine):
    """Test valid user input."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True
        }
    )
    
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Scribe"
    assert result["data"][CONF_DB_URL] == "postgresql+asyncpg://user:pass@host/db"
    
    # Verify engine was created and disposed
    mock_create_async_engine.assert_called_once()
    mock_create_async_engine.return_value.dispose.assert_called_once()

@pytest.mark.asyncio
async def test_flow_user_connection_error(hass, mock_create_async_engine):
    """Test user input with connection error."""
    # Mock connection failure
    mock_create_async_engine.return_value.connect.side_effect = Exception("Connection failed")
    
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True
        }
    )
    
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"

@pytest.mark.asyncio
async def test_flow_user_must_record_something(hass):
    """Test validation that at least one thing must be recorded."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_RECORD_STATES: False,
            CONF_RECORD_EVENTS: False
        }
    )
    
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "must_record_something"

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
    assert result["reason"] == "already_configured"

    # Try to import
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT},
        data={CONF_DB_URL: "postgresql://new/db"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"

@pytest.mark.asyncio
async def test_flow_user_asyncpg_replacement(hass, mock_db_connection, mock_engine):
    """Test that postgresql:// is replaced with postgresql+asyncpg://."""
    from custom_components.scribe.const import DOMAIN
    from homeassistant.data_entry_flow import FlowResultType
    
    # Configure mock_engine to return mock_db_connection
    mock_engine.connect.return_value.__aenter__.return_value = mock_db_connection
    
    user_input = {
        CONF_DB_URL: "postgresql://user:pass@host/db",
        CONF_RECORD_STATES: True
    }
    
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data=user_input
    )
    
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == "postgresql+asyncpg://user:pass@host/db"

@pytest.mark.asyncio
async def test_options_flow(hass, mock_config_entry):
    """Test options flow."""
    from custom_components.scribe.config_flow import ScribeOptionsFlowHandler
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
