"""The config flow: what a user is told before Scribe is ever set up.

Everything here is what stands between a mistyped URL — or a database that
cannot do the job — and an installation that looks fine while recording
nothing.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.scribe.config_flow import _coerce_options
from custom_components.scribe.const import (
    CONF_DB_URL,
    CONF_EXCLUDE_DOMAINS,
    CONF_RECORD_EVENTS,
    CONF_RECORD_STATES,
    DOMAIN,
)

GOOD_URL = "postgresql://scribe:secret@127.0.0.1:5432/scribe"


def _check_database(result):
    """Answer the flow's database check without touching a database."""
    return patch(
        "custom_components.scribe.config_flow._check_database",
        new_callable=AsyncMock,
        return_value=result,
    )


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


@pytest.mark.asyncio
async def test_a_usable_database_creates_the_entry(hass):
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with _check_database(None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == GOOD_URL


@pytest.mark.asyncio
async def test_an_empty_url_is_reported_on_the_field(hass):
    """The message belongs next to the box the user left blank."""
    result = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DB_URL: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DB_URL: "cannot_connect"}


@pytest.mark.asyncio
async def test_an_unreachable_database_is_reported(hass):
    result = await _start_user_flow(hass)

    with _check_database("cannot_connect"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_a_database_without_timescaledb_is_refused(hass):
    """Distinguishable from an unreachable one: they need different fixes."""
    result = await _start_user_flow(hass)

    with _check_database("no_timescaledb"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_timescaledb"}


@pytest.mark.asyncio
async def test_yaml_import_is_refused_without_timescaledb(hass):
    """A YAML setup gets the same gate as the UI, and leaves no entry behind.

    Nothing is stored, so the block is imported again at the next restart — a
    database that was merely missing the extension recovers by fixing it.
    """
    with _check_database("no_timescaledb"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data={CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_timescaledb"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_yaml_import_creates_the_entry_when_the_database_is_usable(hass):
    with _check_database(None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data={CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == GOOD_URL


@pytest.mark.asyncio
async def test_a_second_setup_is_refused(hass, mock_config_entry):
    """Scribe is single-instance: the form is never even shown a second time.

    `single_config_entry` in the manifest makes Home Assistant abort the flow
    before the first step, so an existing installation is configured through
    its options rather than set up again.
    """
    mock_config_entry.add_to_hass(hass)

    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_recording_nothing_at_all_is_refused(hass, mock_config_entry):
    """An install that records neither states nor events would be inert."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECORD_STATES: False, CONF_RECORD_EVENTS: False},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "must_record_something"}


def test_a_single_filter_value_is_accepted_as_a_list():
    """The UI hands back a bare string when only one value was entered."""
    coerced = _coerce_options({CONF_EXCLUDE_DOMAINS: "sensor"})
    assert coerced[CONF_EXCLUDE_DOMAINS] == ["sensor"]


def test_an_empty_filter_value_becomes_an_empty_list():
    coerced = _coerce_options({CONF_EXCLUDE_DOMAINS: ""})
    assert coerced[CONF_EXCLUDE_DOMAINS] == []
