"""Unit tests for ScribeWriter.rename_entity collision handling.

These mock asyncpg and assert on the SQL issued, so they run anywhere.
test_rename_integration.py covers the same paths against a real TimescaleDB
and asserts on the resulting table contents instead.
"""
import asyncio

import pytest
from unittest.mock import MagicMock, patch

import asyncpg

from homeassistant.helpers import issue_registry as ir

from custom_components.scribe.const import DOMAIN
from custom_components.scribe.writer import ScribeWriter


@pytest.fixture
async def writer(hass, mock_pool):
    """Create a writer instance backed by the mocked pool."""
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=2,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert=None,
        ssl_cert_file=None,
        ssl_key_file=None,
    )
    writer._pool = mock_pool
    yield writer
    if writer._task:
        await writer.stop()


def _executed_sql(mock_db_connection):
    """Return the list of (sql, args) tuples executed on the mock connection."""
    return [
        (call.args[0], call.args[1:])
        for call in mock_db_connection.execute.mock_calls
        if call.args
    ]


def _raise_unique_violation_once(mock_db_connection):
    """Make the first entities UPDATE raise UniqueViolationError, others succeed."""
    calls = {"n": 0}

    async def execute_side_effect(sql, *args):
        if "UPDATE entities SET entity_id" in sql:
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncpg.UniqueViolationError("duplicate key")
        return "UPDATE 1"

    mock_db_connection.execute.side_effect = execute_side_effect


def _set_rows(mock_db_connection, occupant, source={"id": 17, "unique_id": "uid-src"}):
    """Serve `occupant` for the destination lookup and `source` for the origin.

    rename_entity fetches both rows and compares their unique_ids, so the two
    must be distinguishable — a single return_value would look like the same
    entity twice (a self-collision).
    """
    async def fetchrow_side_effect(sql, *args):
        if args and args[0] == "sensor.new":
            return occupant
        if args and args[0] == "sensor.old":
            return source
        return None

    mock_db_connection.fetchrow.side_effect = fetchrow_side_effect


@pytest.mark.asyncio
async def test_rename_free_target(writer, mock_db_connection):
    """Target name free: single UPDATE on entities, nothing else touched."""
    mock_db_connection.execute.return_value = "UPDATE 1"
    writer._entity_id_map["sensor.old"] = 17
    writer._metadata_id_map[17] = "sensor.old"

    await writer.rename_entity("sensor.old", "sensor.new")

    executed = _executed_sql(mock_db_connection)
    assert executed == [
        ("UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
         ("sensor.new", "sensor.old")),
    ]
    # Cache invalidated, not remapped — re-resolved lazily on next write.
    assert "sensor.old" not in writer._entity_id_map
    assert 17 not in writer._metadata_id_map


@pytest.mark.asyncio
async def test_rename_refuses_live_occupant(writer, mock_db_connection):
    """Occupant is a different entity that resolves live: refuse, modify nothing."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, {
        "id": 42, "unique_id": "uid-b", "domain": "sensor", "platform": "mqtt",
    })
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.new"

    with patch(
        "custom_components.scribe.writer.er.async_get", return_value=registry
    ):
        await writer.rename_entity("sensor.old", "sensor.new")

    registry.async_get_entity_id.assert_called_once_with("sensor", "mqtt", "uid-b")
    # Only the failed initial UPDATE was attempted — no merge, no delete.
    executed = _executed_sql(mock_db_connection)
    assert len(executed) == 1
    assert "UPDATE entities" in executed[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "occupant",
    [
        {"id": 42, "unique_id": None, "domain": "sensor", "platform": "mqtt"},
        {"id": 42, "unique_id": "uid-b", "domain": None, "platform": "mqtt"},
        {"id": 42, "unique_id": "uid-b", "domain": "sensor", "platform": None},
    ],
    ids=["no-unique-id", "no-domain", "no-platform"],
)
async def test_rename_refuses_unprovable_occupant(writer, mock_db_connection, occupant):
    """Any missing registry coordinate makes death unprovable: refuse."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, occupant)
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None  # must not be trusted

    with patch(
        "custom_components.scribe.writer.er.async_get", return_value=registry
    ):
        await writer.rename_entity("sensor.old", "sensor.new")

    # The registry must not even be consulted with partial coordinates.
    registry.async_get_entity_id.assert_not_called()
    executed = _executed_sql(mock_db_connection)
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_rename_reuses_dead_orphan(writer, mock_db_connection):
    """Provably dead occupant: history merged, orphan row deleted, rename done."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, {
        "id": 42, "unique_id": "uid-dead", "domain": "sensor", "platform": "mqtt",
    })
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None  # dead: absent from registry
    writer._entity_id_map["sensor.old"] = 17
    writer._metadata_id_map[17] = "sensor.old"
    writer._entity_id_map["sensor.new"] = 42
    writer._metadata_id_map[42] = "sensor.new"

    with patch(
        "custom_components.scribe.writer.er.async_get", return_value=registry
    ):
        await writer.rename_entity("sensor.old", "sensor.new")

    executed = _executed_sql(mock_db_connection)
    # 1: failed rename, 2: drop colliding timestamps, 3: merge history,
    # 4: delete occupant row, 5: rename.
    assert "DELETE FROM states_raw" in executed[1][0]
    assert executed[1][1] == (17, 42)
    assert executed[2] == (
        "UPDATE states_raw SET metadata_id = $1 WHERE metadata_id = $2", (17, 42))
    assert executed[3] == ("DELETE FROM entities WHERE id = $1", (42,))
    assert executed[4] == (
        "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
        ("sensor.new", "sensor.old"))
    assert len(executed) == 5
    # Both cache entries invalidated (old id and absorbed occupant id).
    assert "sensor.old" not in writer._entity_id_map
    assert "sensor.new" not in writer._entity_id_map
    assert 17 not in writer._metadata_id_map
    assert 42 not in writer._metadata_id_map


@pytest.mark.asyncio
async def test_rename_self_collision_merges(hass, writer, mock_db_connection):
    """Both rows carry the same unique_id: one entity, two rows — merge.

    Reproduces the race seen live on 3.7.0b1: a concurrent registry-sync task
    inserted the destination row for the very entity being renamed. Note the
    occupant resolves live to the destination — exactly like a different entity
    legitimately living there — so only the matching unique_ids separate the
    two cases.
    """
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(
        mock_db_connection,
        occupant={"id": 42, "unique_id": "uid-self",
                  "domain": "sensor", "platform": "mqtt"},
        source={"id": 17, "unique_id": "uid-self"},
    )
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.new"

    with patch(
        "custom_components.scribe.writer.er.async_get", return_value=registry
    ):
        await writer.rename_entity("sensor.old", "sensor.new")

    executed = _executed_sql(mock_db_connection)
    # Same merge sequence as the dead-orphan path.
    assert executed[2] == (
        "UPDATE states_raw SET metadata_id = $1 WHERE metadata_id = $2", (17, 42))
    assert executed[3] == ("DELETE FROM entities WHERE id = $1", (42,))
    assert len(executed) == 5
    # No repair issue: nothing was refused.
    assert _get_issue(hass) is None


@pytest.mark.asyncio
async def test_rename_waits_for_metadata_lock(writer, mock_db_connection):
    """rename_entity serializes behind the metadata lock (no interleaving)."""
    mock_db_connection.execute.return_value = "UPDATE 1"
    async with writer._metadata_lock:
        task = asyncio.ensure_future(
            writer.rename_entity("sensor.old", "sensor.new"))
        await asyncio.sleep(0)
        # Lock held elsewhere: the rename must not have touched the DB yet.
        mock_db_connection.execute.assert_not_called()
    await task
    mock_db_connection.execute.assert_called_once()


@pytest.mark.asyncio
async def test_rename_target_freed_meanwhile(writer, mock_db_connection):
    """Occupant vanished between the failed UPDATE and the lookup: plain rename."""
    _raise_unique_violation_once(mock_db_connection)
    mock_db_connection.fetchrow.return_value = None

    await writer.rename_entity("sensor.old", "sensor.new")

    executed = _executed_sql(mock_db_connection)
    assert len(executed) == 2
    assert executed[1] == (
        "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
        ("sensor.new", "sensor.old"))


@pytest.mark.asyncio
async def test_rename_source_vanished(writer, mock_db_connection):
    """Occupant present but the source row is gone: no-op, nothing modified."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(
        mock_db_connection,
        occupant={"id": 42, "unique_id": "uid-b",
                  "domain": "sensor", "platform": "mqtt"},
        source=None,
    )

    await writer.rename_entity("sensor.old", "sensor.new")

    assert len(_executed_sql(mock_db_connection)) == 1


@pytest.mark.asyncio
async def test_rename_no_pool_is_noop(hass, writer, mock_db_connection):
    """Without a pool the call returns immediately."""
    writer._pool = None
    await writer.rename_entity("sensor.old", "sensor.new")
    mock_db_connection.execute.assert_not_called()


def _get_issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, "rename_collision_sensor.new")


@pytest.mark.asyncio
async def test_refusal_live_raises_repair_issue(hass, writer, mock_db_connection):
    """Refusing because of a live occupant surfaces a Repairs issue."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, {
        "id": 42, "unique_id": "uid-b", "domain": "sensor", "platform": "mqtt",
    })
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.still_alive"

    with patch(
        "custom_components.scribe.writer.er.async_get", return_value=registry
    ):
        await writer.rename_entity("sensor.old", "sensor.new")

    issue = _get_issue(hass)
    assert issue is not None
    assert issue.translation_key == "rename_refused_live"
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {
        "old_entity_id": "sensor.old",
        "new_entity_id": "sensor.new",
        "occupant": "sensor.still_alive",
    }


@pytest.mark.asyncio
async def test_refusal_unprovable_raises_repair_issue(hass, writer, mock_db_connection):
    """Refusing because death is unprovable surfaces a Repairs issue."""
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, {
        "id": 42, "unique_id": None, "domain": "sensor", "platform": "mqtt",
    })

    await writer.rename_entity("sensor.old", "sensor.new")

    issue = _get_issue(hass)
    assert issue is not None
    assert issue.translation_key == "rename_refused_unprovable"
    assert issue.translation_placeholders == {
        "old_entity_id": "sensor.old",
        "new_entity_id": "sensor.new",
    }


@pytest.mark.asyncio
async def test_rename_failure_raises_error_issue(hass, writer, mock_db_connection):
    """An unexpected DB error surfaces an ERROR-severity Repairs issue."""
    mock_db_connection.execute.side_effect = RuntimeError("connection lost")

    await writer.rename_entity("sensor.old", "sensor.new")

    issue = _get_issue(hass)
    assert issue is not None
    assert issue.translation_key == "rename_failed"
    assert issue.severity == ir.IssueSeverity.ERROR
    assert "connection lost" in issue.translation_placeholders["error"]


@pytest.mark.asyncio
async def test_successful_rename_clears_repair_issue(hass, writer, mock_db_connection):
    """A later successful rename to the same destination retires the issue."""
    # First attempt: refused (unprovable occupant) -> issue raised.
    _raise_unique_violation_once(mock_db_connection)
    _set_rows(mock_db_connection, {
        "id": 42, "unique_id": None, "domain": None, "platform": None,
    })
    await writer.rename_entity("sensor.old", "sensor.new")
    assert _get_issue(hass) is not None

    # Second attempt: the occupant row is gone, plain rename succeeds.
    mock_db_connection.execute.side_effect = None
    mock_db_connection.execute.return_value = "UPDATE 1"
    mock_db_connection.fetchrow.side_effect = None
    mock_db_connection.fetchrow.return_value = None
    await writer.rename_entity("sensor.old", "sensor.new")
    assert _get_issue(hass) is None
