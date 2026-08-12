"""Regressions found while auditing 3.8.0rc2.

Three defects that shipped: a query service nothing could stop, a startup row
count that scanned every chunk, and raw driver text exposed in a state
attribute. Plus guards for behaviour that was already correct and must stay so.
"""

import asyncio

import pytest

from homeassistant.helpers.entity_component import async_update_entity

from custom_components.scribe.const import (
    CONF_EXCLUDE_EVENTS,
    CONF_INCLUDE_EVENTS,
    DOMAIN,
)
from custom_components.scribe.writer import EXACT_COUNT_CEILING, QUERY_TIMEOUT_MS

from .conftest import write_states


@pytest.mark.asyncio
async def test_query_service_is_stopped_by_the_server(hass, scribe_entry, monkeypatch):
    """`scribe.query` takes arbitrary SQL from the UI; it must be bounded.

    Without a statement_timeout one careless aggregate pins a pooled
    connection and works the server until it finishes — the shape of the
    freeze this audit started from. The ceiling is lowered here so the test
    stays quick; what is asserted is that the *server* ends the query, not the
    caller giving up.
    """
    _, writer = await scribe_entry()
    assert QUERY_TIMEOUT_MS <= 300_000, "the ceiling must stay a ceiling"

    monkeypatch.setattr("custom_components.scribe.writer.QUERY_TIMEOUT_MS", 500)

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(writer.query("SELECT pg_sleep(30)"), timeout=15)

    assert not isinstance(excinfo.value, asyncio.TimeoutError), (
        "the query ran on: nothing bounds it server-side"
    )
    assert (
        "cancel" in str(excinfo.value).lower()
        or "timeout" in str(excinfo.value).lower()
    ), f"unexpected failure: {excinfo.value!r}"


@pytest.mark.asyncio
async def test_initial_counts_do_not_scan_every_chunk(hass, writer, db, monkeypatch):
    """Startup must not aggregate the whole hypertable.

    On a year-old install `SELECT count(*)` walks tens of millions of rows and
    decompresses every chunk, at every Home Assistant start. The estimate from
    the planner statistics must be tried first; the exact count survives only
    as a fallback for when those statistics are not there yet.
    """
    await write_states(writer, "sensor.counted", 5)
    issued = []

    real_fetchval = writer._fetchval

    async def spy(sql, *args):
        issued.append(sql)
        return await real_fetchval(sql, *args)

    monkeypatch.setattr(writer, "_fetchval", spy)
    await writer._get_initial_counts()

    assert writer._states_written >= 0
    assert any("approximate_row_count" in s for s in issued), (
        "the cheap path was never tried"
    )


@pytest.mark.asyncio
async def test_row_count_falls_back_without_timescaledb(hass, writer, monkeypatch):
    """Plain PostgreSQL has no approximate_row_count; the exact count remains."""
    issued = []
    real_fetchval = writer._fetchval

    async def spy(sql, *args):
        issued.append(sql)
        if "approximate_row_count" in sql:
            raise RuntimeError("function approximate_row_count does not exist")
        return await real_fetchval(sql, *args)

    monkeypatch.setattr(writer, "_fetchval", spy)
    count = await writer._row_count("states_raw", "states")

    assert count == 0
    assert any("count(*)" in s.lower() for s in issued), "no fallback was attempted"


@pytest.mark.asyncio
async def test_last_error_attribute_cannot_leak_the_dsn(hass, scribe_entry):
    """last_error is world-readable in Home Assistant and lands in the recorder."""
    _, writer = await scribe_entry()

    writer._last_error = (
        "connection failed: postgresql://postgres:scribe@127.0.0.1:55432/scribe"
    )
    await async_update_entity(hass, "binary_sensor.scribe_database_connection")
    await hass.async_block_till_done()

    attrs = hass.states.get("binary_sensor.scribe_database_connection").attributes
    assert "scribe@127.0.0.1" not in str(attrs), (
        "database credentials exposed in a state attribute"
    )
    assert "redacted" in str(attrs["last_error"]).lower()
    # The rest of the message must survive, or the attribute is useless.
    assert "connection failed" in attrs["last_error"]


@pytest.mark.asyncio
async def test_connection_sensor_reflects_a_lost_database(hass, scribe_entry):
    """The connectivity entity must not stay green once writing fails."""
    _, writer = await scribe_entry()
    assert hass.states.get("binary_sensor.scribe_database_connection").state == "on"

    writer._connected = False
    await async_update_entity(hass, "binary_sensor.scribe_database_connection")
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.scribe_database_connection").state == "off"


@pytest.mark.asyncio
async def test_reload_does_not_double_record(hass, scribe_entry):
    """A reload must not leave the previous bus listener attached."""
    entry, _ = await scribe_entry()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    writer = hass.data[DOMAIN][entry.entry_id]["writer"]
    hass.states.async_set("sensor.after_reload", "1")
    await hass.async_block_till_done()

    queued = [i for i in writer._queue if i.get("entity_id") == "sensor.after_reload"]
    assert len(queued) == 1, f"state enqueued {len(queued)}x after one reload"


@pytest.mark.asyncio
async def test_exclude_events_beats_include_events_on_overlap(hass, scribe_entry):
    """Documented precedence: exclude wins where the two lists overlap."""
    _, writer = await scribe_entry(
        **{
            CONF_INCLUDE_EVENTS: ["audit_kept", "audit_dropped"],
            CONF_EXCLUDE_EVENTS: ["audit_dropped"],
        }
    )

    hass.bus.async_fire("audit_kept", {})
    hass.bus.async_fire("audit_dropped", {})
    await hass.async_block_till_done()
    await writer._flush()

    rows = await writer.query("SELECT DISTINCT event_type FROM events")
    types = {r["event_type"] for r in rows}
    assert "audit_kept" in types
    assert "audit_dropped" not in types


@pytest.mark.asyncio
async def test_small_tables_are_still_counted_exactly(hass, writer, db, monkeypatch):
    """Precision is only traded away once counting actually costs something."""
    await write_states(writer, "sensor.small", 7)
    issued = []
    real_fetchval = writer._fetchval

    async def spy(sql, *args):
        issued.append(sql)
        return await real_fetchval(sql, *args)

    monkeypatch.setattr(writer, "_fetchval", spy)
    count = await writer._row_count("states_raw", "states")

    assert count == 7, "a small table must report its real size"
    assert any("count(*)" in s.lower() for s in issued)


@pytest.mark.asyncio
async def test_large_tables_use_the_estimate(hass, writer, monkeypatch):
    """Past the ceiling the exact count must not be attempted at all."""
    issued = []

    async def spy(sql, *args):
        issued.append(sql)
        if "approximate_row_count" in sql:
            return EXACT_COUNT_CEILING + 1
        raise AssertionError(f"unexpected query past the ceiling: {sql}")

    monkeypatch.setattr(writer, "_fetchval", spy)
    count = await writer._row_count("states_raw", "states")

    assert count == EXACT_COUNT_CEILING + 1
    assert not any("count(*)" in s.lower() for s in issued)
