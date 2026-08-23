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
        "legacy_schema",
        "retention_failed",
        "schema_failed",
        "view_failed",
        "no_hypertable",
        "no_compression",
        "ssl_degraded",
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


@pytest.mark.asyncio
async def test_schema_failure_raises_an_issue(hass, clean_db, monkeypatch):
    """A database that answers but refuses the schema records nothing."""
    from custom_components.scribe.writer import ISSUE_SCHEMA_FAILED, ScribeWriter

    async def boom(self, conn):
        raise PermissionError("permission denied for schema public")

    monkeypatch.setattr(ScribeWriter, "_init_entities_table", boom)

    w = make_writer(hass)
    await w.start()
    try:
        issue = get_issue(hass, ISSUE_SCHEMA_FAILED)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.ERROR
        assert "permission denied" in issue.translation_placeholders["error"]
        assert w._connected is False
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_schema_issue_clears_on_a_healthy_start(hass, writer):
    """The normal fixture start must leave no schema issue behind."""
    from custom_components.scribe.writer import ISSUE_SCHEMA_FAILED, ISSUE_VIEW_FAILED

    assert get_issue(hass, ISSUE_SCHEMA_FAILED) is None
    assert get_issue(hass, ISSUE_VIEW_FAILED) is None


@pytest.mark.asyncio
async def test_view_failure_raises_an_issue(hass, clean_db, monkeypatch):
    """History keeps being written, but nothing can read it back."""
    import asyncpg

    from custom_components.scribe.writer import ISSUE_VIEW_FAILED

    original = asyncpg.Connection.execute

    async def refuse_view(self, query, *args, **kwargs):
        if "CREATE VIEW" in str(query):
            raise PermissionError("permission denied for schema public")
        return await original(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", refuse_view)

    w = make_writer(hass)
    await w.start()
    try:
        issue = get_issue(hass, ISSUE_VIEW_FAILED)
        assert issue is not None
        assert issue.translation_placeholders["view"] == "states"
    finally:
        monkeypatch.undo()
        await w.stop()


@pytest.mark.asyncio
async def test_plain_table_with_timescaledb_raises_an_issue(hass, writer, db):
    """A table that silently stayed plain grows several times faster."""
    from custom_components.scribe.writer import ISSUE_NO_HYPERTABLE

    async with db.acquire() as conn:
        await conn.execute("CREATE TABLE plain_states (time TIMESTAMPTZ NOT NULL)")
    try:
        await writer._verify_storage_features("plain_states")

        issue = get_issue(hass, ISSUE_NO_HYPERTABLE.format(table="plain_states"))
        assert issue is not None
        assert issue.translation_placeholders["table"] == "plain_states"
    finally:
        async with db.acquire() as conn:
            await conn.execute("DROP TABLE plain_states")


@pytest.mark.asyncio
async def test_missing_compression_policy_raises_an_issue(hass, writer, db):
    """Chunked but never compressed is a database several times too big."""
    from custom_components.scribe.writer import ISSUE_NO_COMPRESSION

    issue_id = ISSUE_NO_COMPRESSION.format(table="states_raw")
    async with db.acquire() as conn:
        await conn.execute(
            "SELECT remove_compression_policy('states_raw', if_exists => true)"
        )

    await writer._verify_storage_features("states_raw")
    assert get_issue(hass, issue_id) is not None

    # And it retires itself once the policy is back.
    await writer._apply_compression_policy("states_raw")
    await writer._verify_storage_features("states_raw")
    assert get_issue(hass, issue_id) is None


@pytest.mark.asyncio
async def test_healthy_hypertables_raise_nothing(hass, writer):
    """The verifier must be invisible on a correctly set up database."""
    from custom_components.scribe.writer import (
        ISSUE_NO_COMPRESSION,
        ISSUE_NO_HYPERTABLE,
    )

    for table in ("states_raw", "events"):
        await writer._verify_storage_features(table)
        assert get_issue(hass, ISSUE_NO_HYPERTABLE.format(table=table)) is None
        assert get_issue(hass, ISSUE_NO_COMPRESSION.format(table=table)) is None
