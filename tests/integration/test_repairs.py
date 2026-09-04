"""Repairs issues: each condition must raise one, and resolving it must retire it.

An issue that never clears is worse than no issue at all — users learn to
ignore the Repairs panel — so every test here checks both directions.
"""

import pytest

from homeassistant.helpers import issue_registry as ir

from custom_components.scribe.const import DOMAIN
from custom_components.scribe.writer import (
    ISSUE_BUFFER_FULL,
    ISSUE_DATA_DROPPED,
    ISSUE_DB_UNREACHABLE,
    ISSUE_LEGACY_SCHEMA,
    ISSUE_NO_TIMESCALEDB,
    ISSUE_WRITE_FAILING,
    WRITE_FAILURE_ISSUE_THRESHOLD,
)

from .conftest import BASE_TIME, make_writer, reconnect

UNREACHABLE_DSN = "postgresql://postgres:scribe@127.0.0.1:1/scribe"


def get_issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _state(entity_id="sensor.repairs", seconds=0):
    from datetime import timedelta

    return {
        "type": "state",
        "time": BASE_TIME + timedelta(seconds=seconds),
        "entity_id": entity_id,
        "state": "on",
        "value": None,
        "attributes": {},
    }


@pytest.mark.asyncio
async def test_unreachable_database_raises_an_issue(hass, clean_db):
    """A database that refuses connections is reported, not just logged."""
    w = make_writer(hass, db_url=UNREACHABLE_DSN)
    await w.start()
    try:
        issue = get_issue(hass, ISSUE_DB_UNREACHABLE)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_key == "db_unreachable"
        assert "error" in issue.translation_placeholders
        # Still running, and still buffering: the connection is retried in the
        # background instead of being given up on until the next restart.
        assert w.running
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_connecting_successfully_clears_the_issue(hass, clean_db):
    """The issue must not outlive the outage that caused it."""
    failing = make_writer(hass, db_url=UNREACHABLE_DSN)
    await failing.start()
    assert get_issue(hass, ISSUE_DB_UNREACHABLE) is not None
    await failing.stop()  # it now retries in the background until told to stop

    working = make_writer(hass)
    await working.start()
    try:
        assert get_issue(hass, ISSUE_DB_UNREACHABLE) is None
    finally:
        await working.stop()


@pytest.mark.asyncio
async def test_repeated_write_failures_raise_an_issue(hass, clean_db):
    """One failed flush is a blip; several in a row means recording stopped."""
    w = make_writer(hass)
    await w.start()
    try:
        await w._pool.close()

        for i in range(WRITE_FAILURE_ISSUE_THRESHOLD - 1):
            w._queue.append(_state(seconds=i))
            await w._flush()
            assert get_issue(hass, ISSUE_WRITE_FAILING) is None, "raised too early"

        w._queue.append(_state(seconds=99))
        await w._flush()

        issue = get_issue(hass, ISSUE_WRITE_FAILING)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_placeholders["failures"] == str(
            WRITE_FAILURE_ISSUE_THRESHOLD
        )
    finally:
        w._pool = None
        await w.stop()


@pytest.mark.asyncio
async def test_a_successful_write_clears_the_failure_issue(hass, clean_db):
    """Recovery retires the issue without needing a restart."""
    w = make_writer(hass)
    await w.start()
    try:
        await w._pool.close()
        for i in range(WRITE_FAILURE_ISSUE_THRESHOLD):
            w._queue.append(_state(seconds=i))
            await w._flush()
        assert get_issue(hass, ISSUE_WRITE_FAILING) is not None

        await reconnect(w)
        w._queue.clear()
        w._queue.append(_state(seconds=200))
        await w._flush()

        assert get_issue(hass, ISSUE_WRITE_FAILING) is None
        assert get_issue(hass, ISSUE_DB_UNREACHABLE) is None
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_full_buffer_raises_an_issue(hass, clean_db):
    """Once the buffer saturates, history is being dropped — say so."""
    w = make_writer(hass, max_queue_size=5)
    await w.start()
    try:
        await w._pool.close()
        for i in range(20):
            w._queue.append(_state(seconds=i))
        await w._flush()

        issue = get_issue(hass, ISSUE_BUFFER_FULL)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_placeholders["max_queue_size"] == "5"
    finally:
        w._pool = None
        await w.stop()


@pytest.mark.asyncio
async def test_dropping_data_without_buffering_raises_an_issue(hass, clean_db):
    """With buffering disabled the loss is immediate and must be visible."""
    w = make_writer(hass, buffer_on_failure=False)
    await w.start()
    try:
        await w._pool.close()
        w._queue.append(_state())
        w._queue.append(_state(seconds=1))
        await w._flush()

        issue = get_issue(hass, ISSUE_DATA_DROPPED)
        assert issue is not None
        assert issue.translation_placeholders["dropped"] == "2"
    finally:
        w._pool = None
        await w.stop()


@pytest.mark.asyncio
async def test_no_issue_on_a_healthy_writer(hass, writer, db):
    """A working setup must leave the Repairs panel empty."""
    writer._queue.append(_state())
    await writer._flush()

    registry = ir.async_get(hass)
    scribe_issues = [
        i for (domain, _), i in registry.issues.items() if domain == DOMAIN
    ]
    assert scribe_issues == []


@pytest.mark.asyncio
async def test_timescaledb_present_raises_nothing(writer, hass):
    """The extension is installed here, so no warning is due."""
    assert get_issue(hass, ISSUE_NO_TIMESCALEDB) is None


@pytest.mark.asyncio
async def test_missing_timescaledb_raises_an_issue(hass, clean_db, monkeypatch):
    """On plain PostgreSQL, compression silently does nothing — warn about it."""
    w = make_writer(hass)
    await w.start()
    try:
        # Scribe enables the extension itself when it can, so the issue only
        # belongs to a database where that is impossible too.
        async def cannot_be_enabled(conn):
            return False

        monkeypatch.setattr(
            "custom_components.scribe.writer.ensure_timescaledb", cannot_be_enabled
        )

        await w._check_timescaledb_available()

        issue = get_issue(hass, ISSUE_NO_TIMESCALEDB)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_key == "no_timescaledb"

        # And it retires once the extension is there.
        monkeypatch.undo()
        await w._check_timescaledb_available()
        assert get_issue(hass, ISSUE_NO_TIMESCALEDB) is None
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_legacy_schema_raises_an_issue(hass, clean_db):
    """A database Scribe refuses to convert must say so, and say what to do."""
    import asyncpg

    from .conftest import DSN

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP VIEW IF EXISTS states CASCADE")
        await conn.execute(
            "CREATE TABLE states (time TIMESTAMPTZ NOT NULL, entity_id TEXT)"
        )
    finally:
        await conn.close()

    w = make_writer(hass)
    await w.start()
    try:
        issue = get_issue(hass, ISSUE_LEGACY_SCHEMA)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert issue.translation_placeholders["version"] == "3.8"
    finally:
        await w.stop()


def test_every_issue_has_translations():
    """A raised issue with no strings entry renders as a blank card.

    The keys are read out of the source rather than listed here. The list this
    replaces was hand-maintained and had already gone stale: `schema_unavailable`
    was raised, translated, and not checked by anything.
    """
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "custom_components" / "scribe"
    source = "\n".join(p.read_text() for p in root.glob("*.py"))

    # The translation key is the second argument of every report call.
    keys = set(
        re.findall(r'_report_issue\(\s*\n?\s*[^,]+,\s*\n?\s*"([a-z_]+)"', source)
    ) | set(
        re.findall(r'_report_rename_issue\(\s*\n?\s*[^,]+,\s*\n?\s*"([a-z_]+)"', source)
    )
    assert len(keys) >= 15, f"the report calls stopped being readable: {keys}"

    for name in (
        "strings.json",
        "translations/en.json",
        "translations/fr.json",
        "translations/es.json",
        "translations/de.json",
    ):
        issues = json.loads((root / name).read_text()).get("issues", {})
        assert keys <= set(issues), f"{name} is missing {sorted(keys - set(issues))}"
        for key in keys:
            assert issues[key].get("title"), f"{name}:{key} has no title"
            assert issues[key].get("description"), f"{name}:{key} has no description"


@pytest.mark.asyncio
async def test_dropping_data_stops_being_reported_once_writing_recovers(hass, clean_db):
    """Every issue retires itself once the condition is gone; this one did not.

    A single drop left the card up for the life of the Home Assistant process,
    long after the database came back and recording resumed — the one issue
    that never cleared, against a README that promises they all do.
    """
    w = make_writer(hass, buffer_on_failure=False)
    await w.start()
    pool = w._pool
    try:
        await pool.close()
        w._queue.append(_state())
        await w._flush()
        assert get_issue(hass, ISSUE_DATA_DROPPED) is not None

        # The database comes back and a write succeeds.
        w._pool = None
        w._next_connect_attempt = 0
        assert await w._ensure_connected() is True
        w._queue.append(_state(seconds=5))
        await w._flush()

        assert get_issue(hass, ISSUE_DATA_DROPPED) is None
    finally:
        await w.stop()
