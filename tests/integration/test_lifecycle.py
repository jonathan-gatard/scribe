"""The integration as Home Assistant runs it: setup, bus, filters, services.

Every other integration module pokes the writer directly. These tests go the
whole way round — a real config entry, real state changes on the real event
bus, through the real filters, into the real database.
"""
import pytest

from homeassistant.core import HomeAssistantError

from custom_components.scribe.const import (
    CONF_ENABLE_STATS_CHUNK,
    CONF_ENABLE_STATS_SIZE,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_EXCLUDE_EVENTS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    DOMAIN,
)


async def _flush(hass, writer):
    """Drain whatever the bus handlers queued."""
    await hass.async_block_till_done()
    await writer._flush()


async def _recorded_entities(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT entity_id FROM states ORDER BY entity_id")
    return [r["entity_id"] for r in rows]


@pytest.mark.asyncio
async def test_setup_creates_schema_and_services(hass, scribe_entry):
    """A successful setup leaves a usable database and registered services."""
    entry, writer = await scribe_entry()

    assert writer.running
    assert hass.services.has_service(DOMAIN, "flush")
    assert hass.services.has_service(DOMAIN, "query")
    async with writer._pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'states_raw')")


@pytest.mark.asyncio
async def test_state_changes_reach_the_database(hass, scribe_entry):
    """The real path: hass.states.async_set -> bus -> filter -> queue -> COPY."""
    entry, writer = await scribe_entry()

    hass.states.async_set("sensor.living_room", "21.5", {"unit": "°C"})
    hass.states.async_set("sensor.living_room", "22.0", {"unit": "°C"})
    await _flush(hass, writer)

    rows = await writer.query(
        "SELECT state, value, attributes FROM states "
        "WHERE entity_id = 'sensor.living_room' ORDER BY time")
    assert len(rows) == 2
    # Numeric states are parsed into `value`, leaving `state` NULL.
    assert rows[0]["value"] == 21.5
    assert rows[0]["state"] is None
    assert rows[0]["attributes"]["unit"] == "°C"


@pytest.mark.asyncio
async def test_non_numeric_states_keep_their_text(hass, scribe_entry):
    """Anything that will not parse as a float is stored as text instead."""
    entry, writer = await scribe_entry()

    hass.states.async_set("binary_sensor.door", "on")
    await _flush(hass, writer)

    rows = await writer.query(
        "SELECT state, value FROM states WHERE entity_id = 'binary_sensor.door'")
    assert rows[0]["state"] == "on"
    assert rows[0]["value"] is None


@pytest.mark.asyncio
async def test_excluded_entities_never_reach_the_database(hass, scribe_entry):
    """exclude_entities drops matching states before they are queued."""
    entry, writer = await scribe_entry(
        **{CONF_EXCLUDE_ENTITIES: ["sensor.secret"]})

    hass.states.async_set("sensor.secret", "1")
    hass.states.async_set("sensor.public", "2")
    await _flush(hass, writer)

    assert await _recorded_entities(writer._pool) == ["sensor.public"]


@pytest.mark.asyncio
async def test_excluded_domains_never_reach_the_database(hass, scribe_entry):
    entry, writer = await scribe_entry(
        **{CONF_EXCLUDE_DOMAINS: ["binary_sensor"]})

    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("sensor.kept", "1")
    await _flush(hass, writer)

    assert await _recorded_entities(writer._pool) == ["sensor.kept"]


@pytest.mark.asyncio
async def test_include_domains_is_exclusive(hass, scribe_entry):
    """Naming an include list means everything unnamed is dropped."""
    entry, writer = await scribe_entry(**{CONF_INCLUDE_DOMAINS: ["sensor"]})

    hass.states.async_set("sensor.kept", "1")
    hass.states.async_set("light.dropped", "on")
    await _flush(hass, writer)

    assert await _recorded_entities(writer._pool) == ["sensor.kept"]


@pytest.mark.asyncio
async def test_exclude_glob_overrides_include_glob(hass, scribe_entry):
    """Regression #33: an exclude glob must win over a matching include glob.

    Home Assistant's own generate_filter lets include_entity_globs
    short-circuit past exclude_entity_globs; Scribe wraps it so an exclude
    match is always a hard reject.
    """
    entry, writer = await scribe_entry(**{
        CONF_INCLUDE_ENTITY_GLOBS: ["sensor.temp_*"],
        CONF_EXCLUDE_ENTITY_GLOBS: ["sensor.temp_private_*"],
    })

    hass.states.async_set("sensor.temp_kitchen", "20")
    hass.states.async_set("sensor.temp_private_bedroom", "21")
    await _flush(hass, writer)

    assert await _recorded_entities(writer._pool) == ["sensor.temp_kitchen"]


@pytest.mark.asyncio
async def test_excluded_attributes_are_stripped(hass, scribe_entry):
    """exclude_attributes removes keys before the row is built, not after."""
    entry, writer = await scribe_entry(
        **{CONF_EXCLUDE_ATTRIBUTES: ["icon", "friendly_name"]})

    hass.states.async_set(
        "sensor.noisy", "1",
        {"icon": "mdi:foo", "friendly_name": "Noisy", "keep": "yes"})
    await _flush(hass, writer)

    rows = await writer.query(
        "SELECT attributes FROM states WHERE entity_id = 'sensor.noisy'")
    assert rows[0]["attributes"] == {"keep": "yes"}


@pytest.mark.asyncio
async def test_events_are_recorded(hass, scribe_entry):
    """Bus events other than state_changed land in the events table."""
    entry, writer = await scribe_entry()

    hass.bus.async_fire("my_custom_event", {"payload": 42})
    await _flush(hass, writer)

    rows = await writer.query(
        "SELECT event_type, event_data FROM events "
        "WHERE event_type = 'my_custom_event'")
    assert len(rows) == 1
    assert rows[0]["event_data"]["payload"] == 42


@pytest.mark.asyncio
async def test_excluded_events_are_dropped(hass, scribe_entry):
    entry, writer = await scribe_entry(
        **{CONF_EXCLUDE_EVENTS: ["boring_event"]})

    hass.bus.async_fire("boring_event", {})
    hass.bus.async_fire("interesting_event", {})
    await _flush(hass, writer)

    rows = await writer.query("SELECT DISTINCT event_type FROM events")
    types = {r["event_type"] for r in rows}
    assert "boring_event" not in types
    assert "interesting_event" in types


@pytest.mark.asyncio
async def test_flush_service_writes_pending_states(hass, scribe_entry):
    """scribe.flush drains the queue without waiting for the interval."""
    entry, writer = await scribe_entry()

    hass.states.async_set("sensor.service_flushed", "7")
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "flush", {}, blocking=True)

    rows = await writer.query(
        "SELECT count(*) AS n FROM states WHERE entity_id = 'sensor.service_flushed'")
    assert rows[0]["n"] == 1


@pytest.mark.asyncio
async def test_query_service_returns_rows(hass, scribe_entry):
    """scribe.query hands structured results back to the caller."""
    entry, writer = await scribe_entry()
    hass.states.async_set("sensor.queried", "3")
    await _flush(hass, writer)

    response = await hass.services.async_call(
        DOMAIN, "query",
        {"sql": "SELECT entity_id, value FROM states WHERE entity_id = 'sensor.queried'"},
        blocking=True, return_response=True)

    assert response["result"][0]["entity_id"] == "sensor.queried"
    assert response["result"][0]["value"] == 3.0


@pytest.mark.asyncio
async def test_query_service_reports_errors(hass, scribe_entry):
    """A failing query surfaces as HomeAssistantError, not a silent empty list."""
    entry, writer = await scribe_entry()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "query", {"sql": "SELECT * FROM nope"},
            blocking=True, return_response=True)


@pytest.mark.asyncio
async def test_unload_stops_recording(hass, scribe_entry):
    """After unload the bus listeners are gone and nothing more is written."""
    entry, writer = await scribe_entry()
    hass.states.async_set("sensor.before_unload", "1")
    await _flush(hass, writer)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not writer.running
    hass.states.async_set("sensor.after_unload", "2")
    await hass.async_block_till_done()
    assert len(writer._queue) == 0


@pytest.mark.asyncio
async def test_stats_coordinators_are_off_by_default(hass, scribe_entry):
    """Statistics polling is opt-in: no coordinator unless it was enabled."""
    entry, writer = await scribe_entry()

    assert hass.data[DOMAIN][entry.entry_id]["chunk_coordinator"] is None
    assert hass.data[DOMAIN][entry.entry_id]["size_coordinator"] is None


@pytest.mark.asyncio
async def test_enabled_stats_coordinator_reads_the_real_database(hass, scribe_entry):
    """Once enabled, the coordinator's data comes from TimescaleDB itself."""
    entry, writer = await scribe_entry(**{
        CONF_ENABLE_STATS_CHUNK: True,
        CONF_ENABLE_STATS_SIZE: True,
    })
    hass.states.async_set("sensor.for_stats", "1")
    await _flush(hass, writer)

    chunk = hass.data[DOMAIN][entry.entry_id]["chunk_coordinator"]
    size = hass.data[DOMAIN][entry.entry_id]["size_coordinator"]
    await chunk.async_refresh()
    await size.async_refresh()

    assert chunk.data["states_total_chunks"] >= 1
    assert size.data["states_total_size"] > 0


@pytest.mark.asyncio
async def test_stats_sensor_entities_report_values(hass, scribe_entry):
    """The sensor platform surfaces coordinator data as entity states."""
    entry, writer = await scribe_entry(**{CONF_ENABLE_STATS_CHUNK: True})
    hass.states.async_set("sensor.for_stats", "1")
    await _flush(hass, writer)
    await hass.data[DOMAIN][entry.entry_id]["chunk_coordinator"].async_refresh()
    await hass.async_block_till_done()

    matching = [
        s for s in hass.states.async_all("sensor")
        if s.entity_id.startswith("sensor.scribe_")
        and s.state not in ("unknown", "unavailable")
    ]
    assert matching, "no Scribe statistics sensor reported a value"


@pytest.mark.asyncio
async def test_connection_binary_sensor_reflects_the_pool(hass, scribe_entry):
    """The connectivity entity is on while the writer holds a working pool."""
    entry, writer = await scribe_entry()

    state = hass.states.get("binary_sensor.scribe_database_connection")
    assert state is not None
    assert state.state == "on"
