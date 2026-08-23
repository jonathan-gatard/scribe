"""Detection of a pre-3.0 database.

Converting the old schema was dropped in 3.9 (see `_detect_legacy_schema`).
What is left must be airtight in both directions: a false positive stops
recording on a healthy install, and a miss lets Scribe write into a schema it
cannot build.
"""

import pytest
from unittest.mock import patch

from custom_components.scribe.writer import (
    ISSUE_LEGACY_SCHEMA,
    ScribeWriter,
    WriterConfig,
)


@pytest.fixture
def writer(hass, mock_pool):
    w = ScribeWriter(
        hass,
        WriterConfig(
            db_url="postgresql://user:pass@host/db",
            chunk_interval="7 days",
            compress_after="7 days",
            record_states=True,
            record_events=True,
            batch_size=2,
            flush_interval=5,
            max_queue_size=10,
            buffer_on_failure=True,
            table_name_states="states",
            table_name_events="events",
        ),
    )
    w._pool = mock_pool
    return w


def _schema(states_table=False, states_legacy=False, entities=True, entities_id=True):
    """Answer the detector's existence queries for a given database shape."""

    async def fetchval(sql, *args):
        if "table_name = 'states' AND table_type = 'BASE TABLE'" in sql:
            return states_table
        if "table_name = 'states_legacy'" in sql:
            return states_legacy
        if "table_name = 'entities'" in sql and "columns" not in sql:
            return entities
        if "column_name = 'id'" in sql:
            return entities_id
        return False

    return fetchval


@pytest.mark.asyncio
async def test_healthy_schema_is_not_legacy(writer, mock_db_connection):
    mock_db_connection.fetchval.side_effect = _schema()
    assert await writer._detect_legacy_schema(mock_db_connection) is None


@pytest.mark.asyncio
async def test_empty_database_is_not_legacy(writer, mock_db_connection):
    """A first run has no relations at all and must proceed normally."""
    mock_db_connection.fetchval.side_effect = _schema(entities=False)
    assert await writer._detect_legacy_schema(mock_db_connection) is None


@pytest.mark.asyncio
async def test_states_as_a_base_table_is_legacy(writer, mock_db_connection):
    """On 3.x `states` is a view; a table by that name is pre-3.0 history."""
    mock_db_connection.fetchval.side_effect = _schema(states_table=True)
    assert await writer._detect_legacy_schema(mock_db_connection) == "states"


@pytest.mark.asyncio
async def test_interrupted_migration_is_legacy(writer, mock_db_connection):
    """An older Scribe renamed `states` and never finished the backfill."""
    mock_db_connection.fetchval.side_effect = _schema(states_legacy=True)
    assert await writer._detect_legacy_schema(mock_db_connection) == "states_legacy"


@pytest.mark.asyncio
async def test_text_keyed_entities_is_legacy(writer, mock_db_connection):
    """Writes resolve metadata_ids through entities.id: without it, nothing works."""
    mock_db_connection.fetchval.side_effect = _schema(entities_id=False)
    assert await writer._detect_legacy_schema(mock_db_connection) == "entities"


@pytest.mark.asyncio
async def test_init_db_creates_nothing_on_a_legacy_database(writer, mock_db_connection):
    """The old data must stay untouched so 3.8 can still convert it."""
    mock_db_connection.fetchval.side_effect = _schema(states_table=True)

    await writer.init_db()

    assert not mock_db_connection.execute.mock_calls
    assert writer._legacy_blocked is True
    assert writer._connected is False


@pytest.mark.asyncio
async def test_legacy_database_raises_a_repairs_issue(writer, mock_db_connection):
    mock_db_connection.fetchval.side_effect = _schema(states_legacy=True)

    with patch.object(writer, "_report_issue") as report:
        await writer.init_db()

    assert report.call_args.args[0] == ISSUE_LEGACY_SCHEMA
    assert report.call_args.args[1] == "legacy_schema"
    placeholders = report.call_args.args[2]
    assert placeholders["relation"] == "states_legacy"
    # The whole point of the issue: name the version that can convert it.
    assert placeholders["version"] == "3.8"


@pytest.mark.asyncio
async def test_blocked_writer_stops_queuing(writer, mock_db_connection):
    """Buffering states that can never be written only fills the queue."""
    mock_db_connection.fetchval.side_effect = _schema(states_table=True)
    await writer.init_db()
    writer._running = True

    writer.enqueue({"type": "state", "entity_id": "sensor.x"})

    assert len(writer._queue) == 0


@pytest.mark.asyncio
async def test_healthy_database_still_initializes(writer, mock_db_connection):
    """The gate must not stand in the way of a normal start."""
    mock_db_connection.fetchval.side_effect = _schema()

    await writer.init_db()

    assert writer._legacy_blocked is False
    assert writer._connected is True
    assert any(
        "CREATE TABLE IF NOT EXISTS states_raw" in str(c.args[0])
        for c in mock_db_connection.execute.mock_calls
        if c.args
    )


@pytest.mark.asyncio
async def test_issue_retires_itself_once_converted(writer, mock_db_connection):
    """An issue that never clears teaches users to ignore the Repairs panel."""
    mock_db_connection.fetchval.side_effect = _schema()

    with patch.object(writer, "_clear_issue") as clear:
        await writer.init_db()

    assert ISSUE_LEGACY_SCHEMA in [c.args[0] for c in clear.mock_calls]
