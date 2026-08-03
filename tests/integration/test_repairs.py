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
    ISSUE_MIGRATION_FAILED,
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

    issue = get_issue(hass, ISSUE_DB_UNREACHABLE)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "db_unreachable"
    assert "error" in issue.translation_placeholders
    assert not w.running


@pytest.mark.asyncio
async def test_connecting_successfully_clears_the_issue(hass, clean_db):
    """The issue must not outlive the outage that caused it."""
    failing = make_writer(hass, db_url=UNREACHABLE_DSN)
    await failing.start()
    assert get_issue(hass, ISSUE_DB_UNREACHABLE) is not None

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

        async def pretend_absent(sql, *args):
            if "pg_extension" in sql:
                return False
            return await original(sql, *args)

        original = w._fetchval
        monkeypatch.setattr(w, "_fetchval", pretend_absent)

        await w._check_timescaledb_available()

        issue = get_issue(hass, ISSUE_NO_TIMESCALEDB)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_key == "no_timescaledb"

        # And it retires once the extension shows up.
        monkeypatch.setattr(w, "_fetchval", original)
        await w._check_timescaledb_available()
        assert get_issue(hass, ISSUE_NO_TIMESCALEDB) is None
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_failed_migration_raises_an_issue(hass, writer, monkeypatch):
    """A half-migrated database leaves history invisible; that must be said."""
    from custom_components.scribe import migration

    async def boom(*args, **kwargs):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(migration, "migrate_database", boom)

    # Drive the same handler the HA-started event triggers.
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    issue = get_issue(hass, ISSUE_MIGRATION_FAILED)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert "migration exploded" in issue.translation_placeholders["error"]


@pytest.mark.asyncio
async def test_every_issue_has_translations(hass):
    """A raised issue with no strings entry renders as a blank card."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "custom_components" / "scribe"
    keys = {
        "db_unreachable",
        "write_failing",
        "buffer_full",
        "data_dropped",
        "no_timescaledb",
        "migration_failed",
        "rename_refused_live",
        "rename_refused_unprovable",
        "rename_failed",
    }
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        data = json.loads((root / name).read_text())
        issues = data.get("issues", {})
        assert keys <= set(issues), f"{name} is missing {keys - set(issues)}"
        for key in keys:
            assert issues[key].get("title"), f"{name}:{key} has no title"
            assert issues[key].get("description"), f"{name}:{key} has no description"
