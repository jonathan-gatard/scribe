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
    entry.entry_id = "e1"

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_DB_URL] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_DB_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_DB_USER] == "**REDACTED**"
    assert diag["entry"]["data"]["other"] == "value"
    assert diag["options"]["option"] == "value"


@pytest.mark.asyncio
async def test_diagnostics_reports_what_the_writer_is_doing(hass, mock_pool):
    """Almost every report is "nothing is being recorded" — say why."""
    from custom_components.scribe.const import DOMAIN
    from custom_components.scribe.writer import ScribeWriter, WriterConfig

    writer = ScribeWriter(
        hass,
        WriterConfig(db_url="postgresql://u:p@h/d", retention_states="365 days"),
    )
    writer._pool = mock_pool
    writer._connected = True
    writer._states_written = 42
    writer._queue.append({"type": "state"})

    entry = MagicMock(spec=ConfigEntry)
    entry.as_dict.return_value = {"data": {}}
    entry.options = {}
    entry.entry_id = "e1"
    hass.data[DOMAIN] = {"e1": {"writer": writer}}

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["writer"]["connected"] is True
    assert diag["writer"]["queue"]["size"] == 1
    assert diag["writer"]["written"]["states"] == 42
    assert diag["writer"]["storage"]["retention_states"] == "365 days"
    assert diag["writer"]["storage"]["retention_events"] == "keep forever"


@pytest.mark.asyncio
async def test_diagnostics_never_leak_a_connection_string(hass, mock_pool):
    """`last_error` is raw driver text and can quote the DSN."""
    from custom_components.scribe.const import DOMAIN
    from custom_components.scribe.writer import ScribeWriter, WriterConfig

    writer = ScribeWriter(hass, WriterConfig(db_url="postgresql://u:p@h/d"))
    writer._pool = mock_pool
    writer._last_error = (
        "could not connect to postgresql://scribe:hunter2@10.0.0.5:5432/scribe"
    )

    entry = MagicMock(spec=ConfigEntry)
    entry.as_dict.return_value = {"data": {}}
    entry.options = {}
    entry.entry_id = "e1"
    hass.data[DOMAIN] = {"e1": {"writer": writer}}

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert "hunter2" not in str(diag)
    assert "10.0.0.5" not in str(diag)
    assert "<redacted-database-url>" in diag["writer"]["last_error"]


@pytest.mark.asyncio
async def test_diagnostics_survive_a_failed_setup(hass):
    """A setup that never built a writer must still produce a report."""
    entry = MagicMock(spec=ConfigEntry)
    entry.as_dict.return_value = {"data": {}}
    entry.options = {}
    entry.entry_id = "missing"

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["writer"] == "not started"
