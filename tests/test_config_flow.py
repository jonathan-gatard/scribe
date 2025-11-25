"""Test Scribe config flow."""
from unittest.mock import patch
from homeassistant import config_entries
from custom_components.scribe.const import (
    DOMAIN,
    CONF_DB_HOST,
    CONF_DB_PORT,
    CONF_DB_USER,
    CONF_DB_PASSWORD,
    CONF_DB_NAME,
    CONF_RECORD_STATES,
)

async def test_form(hass):
    """Test we get the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    with patch(
        "custom_components.scribe.config_flow.ScribeConfigFlow.validate_input",
        return_value={"title": "Scribe"},
    ), patch(
        "custom_components.scribe.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_DB_HOST: "localhost",
                CONF_DB_PORT: 5432,
                CONF_DB_USER: "postgres",
                CONF_DB_PASSWORD: "password",
                CONF_DB_NAME: "homeassistant",
                CONF_RECORD_STATES: True,
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == "create_entry"
    assert result2["title"] == "Scribe"
    assert result2["data"] == {
        CONF_DB_HOST: "localhost",
        CONF_DB_PORT: 5432,
        CONF_DB_USER: "postgres",
        CONF_DB_PASSWORD: "password",
        CONF_DB_NAME: "homeassistant",
        CONF_RECORD_STATES: True,
        "record_events": False,
        "enable_statistics": False,
    }
    assert len(mock_setup_entry.mock_calls) == 1
