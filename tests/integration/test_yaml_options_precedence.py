"""YAML must not silently override options the user set in the UI (#52).

A `scribe:` block in configuration.yaml is validated against CONFIG_SCHEMA
before async_setup runs. If that schema carries `default=` values, voluptuous
injects *every* optional key into the validated dict — so `get_config` sees
each one as "present in YAML", which outranks the options flow. The result is
an options page whose toggles are saved, displayed as on, and ignored.
"""

import pytest
import voluptuous as vol

from custom_components.scribe import CONFIG_SCHEMA
from custom_components.scribe.const import (
    CONF_DB_URL,
    CONF_ENABLE_STATS_CHUNK,
    CONF_ENABLE_STATS_IO,
    CONF_ENABLE_STATS_SIZE,
    CONF_EXCLUDE_ENTITIES,
    CONF_RECORD_EVENTS,
    CONF_RECORD_STATES,
    DOMAIN,
)

from .conftest import DSN


def test_yaml_schema_keeps_only_what_the_user_wrote():
    """Validating a minimal YAML block must not invent keys.

    This is the root cause of #52: any key voluptuous adds here is treated
    downstream as an explicit user choice.
    """
    validated = CONFIG_SCHEMA({DOMAIN: {CONF_DB_URL: DSN}})[DOMAIN]

    assert set(validated) == {CONF_DB_URL}, (
        f"schema injected keys the user never wrote: {set(validated) - {CONF_DB_URL}}"
    )


def test_yaml_schema_still_accepts_and_validates_real_values():
    """Stripping the defaults must not stop YAML from configuring anything."""
    validated = CONFIG_SCHEMA({DOMAIN: {CONF_DB_URL: DSN, CONF_ENABLE_STATS_IO: True}})[
        DOMAIN
    ]
    assert validated[CONF_ENABLE_STATS_IO] is True

    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({DOMAIN: {CONF_DB_URL: DSN, CONF_ENABLE_STATS_IO: "yes please"}})


@pytest.fixture
async def yaml_entry(hass, clean_db):
    """Set up the integration the way a YAML user with UI options has it.

    Mirrors #52: configuration.yaml carries only db_url, while the statistics
    toggles were switched on in the options flow.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.scribe import async_setup

    # What Home Assistant hands async_setup: the *validated* YAML.
    validated = CONFIG_SCHEMA({DOMAIN: {CONF_DB_URL: DSN}})
    await async_setup(hass, validated)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DB_URL: DSN},
        options={
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True,
            CONF_ENABLE_STATS_IO: True,
            CONF_ENABLE_STATS_CHUNK: True,
            CONF_ENABLE_STATS_SIZE: True,
        },
        entry_id="yaml_precedence_entry",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield entry

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_ui_statistics_toggles_apply_to_a_yaml_user(hass, yaml_entry):
    """The reported symptom: only the connectivity entity ever appears.

    With the toggles on, the coordinators must exist and the statistics
    sensors must be created.
    """
    data = hass.data[DOMAIN][yaml_entry.entry_id]
    assert data["chunk_coordinator"] is not None, "chunk stats toggle was ignored"
    assert data["size_coordinator"] is not None, "size stats toggle was ignored"

    sensors = [
        s.entity_id
        for s in hass.states.async_all("sensor")
        if s.entity_id.startswith("sensor.scribe_")
    ]
    assert sensors, "no Scribe sensor was created despite every toggle being on"
    # The binary_sensor was the only entity the reporter saw; it is still there.
    assert hass.states.get("binary_sensor.scribe_database_connection") is not None


@pytest.mark.asyncio
async def test_ui_filters_apply_to_a_yaml_user(hass, yaml_entry, hass_storage):
    """Filters set in the UI must survive too — same injected-default trap."""
    hass.config_entries.async_update_entry(
        yaml_entry,
        options={**yaml_entry.options, CONF_EXCLUDE_ENTITIES: ["sensor.dropped"]},
    )
    await hass.async_block_till_done()

    writer = hass.data[DOMAIN][yaml_entry.entry_id]["writer"]
    hass.states.async_set("sensor.dropped", "1")
    hass.states.async_set("sensor.kept", "2")
    await hass.async_block_till_done()
    await writer._flush()

    rows = await writer.query("SELECT DISTINCT entity_id FROM states")
    recorded = {r["entity_id"] for r in rows}
    assert "sensor.kept" in recorded
    assert "sensor.dropped" not in recorded, "UI exclusion was overridden by YAML"


@pytest.mark.asyncio
async def test_yaml_still_wins_when_the_user_actually_sets_it(hass, clean_db):
    """YAML keeps priority for keys the user really wrote — that is the point."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.scribe import async_setup

    validated = CONFIG_SCHEMA({DOMAIN: {CONF_DB_URL: DSN, CONF_ENABLE_STATS_IO: False}})
    await async_setup(hass, validated)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DB_URL: DSN},
        options={
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True,
            CONF_ENABLE_STATS_IO: True,  # UI says on, YAML explicitly says off
        },
        entry_id="yaml_wins_entry",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    try:
        io_sensors = [
            s.entity_id
            for s in hass.states.async_all("sensor")
            if s.entity_id.startswith("sensor.scribe_")
        ]
        assert not io_sensors, "explicit YAML value should have won"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
