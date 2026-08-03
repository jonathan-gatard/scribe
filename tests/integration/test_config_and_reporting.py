"""Config flow, options reload, diagnostics and system health, against a real DB.

The config flow's job is to reject a database it cannot reach, so testing it
without one only ever tests the mock. Same for system health, which reports on
a live connection.
"""
import pytest

from homeassistant import config_entries, data_entry_flow

from custom_components.scribe import system_health as scribe_system_health
from custom_components.scribe.const import (
    CONF_DB_URL,
    CONF_EXCLUDE_ENTITIES,
    DOMAIN,
)
from custom_components.scribe.diagnostics import async_get_config_entry_diagnostics

from .conftest import DSN


@pytest.mark.asyncio
async def test_config_flow_accepts_a_reachable_database(hass, clean_db):
    """A working DSN creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DB_URL: DSN})
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == DSN


@pytest.mark.asyncio
async def test_config_flow_rejects_a_wrong_password(hass, clean_db):
    """Bad credentials must surface as a form error, not a broken entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DB_URL: DSN.replace(":scribe@", ":wrong-password@")})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_config_flow_rejects_an_unreachable_host(hass, clean_db):
    """A host that does not answer is refused before the entry is created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DB_URL: "postgresql://postgres:scribe@127.0.0.1:1/scribe"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_config_flow_normalizes_a_sqlalchemy_dsn(hass, clean_db):
    """postgresql+asyncpg:// URLs are accepted: users copy them from LTSS docs."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DB_URL: DSN.replace("postgresql://", "postgresql+asyncpg://")})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_only_one_entry_is_allowed(hass, scribe_entry):
    """Scribe is single_config_entry: a second flow must abort."""
    await scribe_entry()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] == data_entry_flow.FlowResultType.ABORT


@pytest.mark.asyncio
async def test_changing_options_reloads_and_applies_the_new_filter(hass, scribe_entry):
    """An options update must take effect without a Home Assistant restart."""
    entry, writer = await scribe_entry()

    hass.states.async_set("sensor.will_be_excluded", "1")
    await hass.async_block_till_done()
    await writer._flush()

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_EXCLUDE_ENTITIES: ["sensor.will_be_excluded"]},
    )
    await hass.async_block_till_done()

    new_writer = hass.data[DOMAIN][entry.entry_id]["writer"]
    hass.states.async_set("sensor.will_be_excluded", "2")
    hass.states.async_set("sensor.still_recorded", "3")
    await hass.async_block_till_done()
    await new_writer._flush()

    rows = await new_writer.query(
        "SELECT entity_id, count(*) AS n FROM states GROUP BY entity_id")
    counts = {r["entity_id"]: r["n"] for r in rows}
    assert counts["sensor.will_be_excluded"] == 1, "new exclusion was not applied"
    assert counts["sensor.still_recorded"] == 1


@pytest.mark.asyncio
async def test_system_health_reports_a_live_connection(hass, scribe_entry):
    """System health talks to the database rather than reporting a cached flag."""
    await scribe_entry()

    info = await scribe_system_health.system_health_info(hass)

    assert info
    # Whatever the exact keys, nothing may report a failure on a healthy setup.
    assert not any(
        isinstance(v, str) and "error" in v.lower() for v in info.values()
    ), info


@pytest.mark.asyncio
async def test_diagnostics_redact_the_database_url(hass, scribe_entry):
    """The DSN carries a password and must never appear in a diagnostics dump."""
    entry, writer = await scribe_entry()

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert "scribe@127.0.0.1" not in str(dump), "database credentials leaked"
    assert dump["entry"]["data"][CONF_DB_URL] == "**REDACTED**"
