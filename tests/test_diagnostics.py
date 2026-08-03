"""Test Scribe diagnostics."""

import pytest
from unittest.mock import MagicMock
from homeassistant.config_entries import ConfigEntry
from custom_components.scribe.diagnostics import async_get_config_entry_diagnostics
from custom_components.scribe.const import CONF_DB_URL, CONF_DB_PASSWORD, CONF_DB_USER


@pytest.mark.asyncio
async def test_diagnostics(hass):
    """Test diagnostics redaction."""
    entry = MagicMock(spec=ConfigEntry)
    entry.as_dict.return_value = {
        "data": {
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_DB_PASSWORD: "secret_password",
            CONF_DB_USER: "secret_user",
            "other": "value",
        }
    }
    entry.options = {"option": "value"}

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_DB_URL] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_DB_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_DB_USER] == "**REDACTED**"
    assert diag["entry"]["data"]["other"] == "value"
    assert diag["options"]["option"] == "value"
