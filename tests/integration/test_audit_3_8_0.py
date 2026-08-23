"""Regressions found while auditing 3.8.0rc2.

Three defects that shipped: a query service nothing could stop, a startup row
count that scanned every chunk, and raw driver text exposed in a state
attribute. Plus guards for behaviour that was already correct and must stay so.
"""

import asyncio

import asyncpg
import pytest

from homeassistant.helpers.entity_component import async_update_entity

from custom_components.scribe.const import (
    CONF_EXCLUDE_EVENTS,
    CONF_INCLUDE_EVENTS,
    DOMAIN,
)
from custom_components.scribe.writer import QUERY_TIMEOUT_MS

from .conftest import make_writer, write_states


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

    # A server-side cancellation, not asyncio giving up: anything else means
    # nothing bounds the query on the database side.
    # wait_for only guards the test against hanging forever; the assertion is
    # about what the *database* does, so only the await sits inside raises().
    query = asyncio.wait_for(writer.query("SELECT pg_sleep(30)"), timeout=15)
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        await query

    assert "statement timeout" in str(excinfo.value).lower()
    assert (
        "cancel" in str(excinfo.value).lower()
        or "timeout" in str(excinfo.value).lower()
    ), f"unexpected failure: {excinfo.value!r}"


@pytest.mark.asyncio
async def test_initial_counts_are_skipped_when_nobody_displays_them(
    hass, clean_db, monkeypatch
):
    """Counting every row is only worth it if a sensor shows the result.

    The I/O statistics sensors are opt-in and off by default, and seeding
    their counters aggregates the whole history — 90 million rows over 103
    compressed chunks on a real installation — at every Home Assistant start.
    """
    w = make_writer(hass, enable_stats_io=False)
    await w.start()
    try:
        issued = []
        real_fetchval = w._fetchval

        async def spy(sql, *args):
            issued.append(sql)
            return await real_fetchval(sql, *args)

        monkeypatch.setattr(w, "_fetchval", spy)
        await w._get_initial_counts()

        assert not issued, f"counted rows nobody asked for: {issued}"
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_initial_counts_are_exact_when_someone_does(hass, clean_db, monkeypatch):
    """With the sensors on, the figure must be the real one.

    TimescaleDB's approximate_row_count() derives compressed chunks from
    `reltuples`, which counts batches and assumes each is full: measured at
    1 270 000 against 444 968 actual on a real chunk, 2.85x too high. A
    counter that overstates by three times is worse than a slow one.
    """
    w = make_writer(hass, enable_stats_io=True)
    await w.start()
    try:
        await write_states(w, "sensor.counted", 6)
        issued = []
        real_fetchval = w._fetchval

        async def spy(sql, *args):
            issued.append(sql)
            return await real_fetchval(sql, *args)

        monkeypatch.setattr(w, "_fetchval", spy)
        await w._get_initial_counts()

        assert w._states_written == 6, "the counter must report the real row count"
        assert not any("approximate_row_count" in s for s in issued), (
            "the estimate over-reports on compressed chunks; it must not be used"
        )
    finally:
        await w.stop()


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
