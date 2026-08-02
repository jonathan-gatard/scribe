"""Tests for ScribeWriter.rename_entity collision handling."""
import pytest
from unittest.mock import MagicMock, patch

import asyncpg

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
    """Occupant resolves in the live registry: refuse, modify nothing."""
    _raise_unique_violation_once(mock_db_connection)
    mock_db_connection.fetchrow.return_value = {
        "id": 42, "unique_id": "uid-b", "domain": "sensor", "platform": "mqtt",
    }
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.still_alive"

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
    mock_db_connection.fetchrow.return_value = occupant
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

    async def fetchrow_side_effect(sql, *args):
        if "WHERE entity_id = $1" in sql and args == ("sensor.new",):
            return {"id": 42, "unique_id": "uid-dead",
                    "domain": "sensor", "platform": "mqtt"}
        if "WHERE entity_id = $1" in sql and args == ("sensor.old",):
            return {"id": 17}
        return None

    mock_db_connection.fetchrow.side_effect = fetchrow_side_effect
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
    # 1: failed rename, 2: merge states, 3: delete orphan row, 4: rename.
    assert executed[1] == (
        "UPDATE states_raw SET metadata_id = $1 WHERE metadata_id = $2", (17, 42))
    assert executed[2] == ("DELETE FROM entities WHERE id = $1", (42,))
    assert executed[3] == (
        "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
        ("sensor.new", "sensor.old"))
    assert len(executed) == 4
    # Both cache entries invalidated (old id and absorbed orphan id).
    assert "sensor.old" not in writer._entity_id_map
    assert "sensor.new" not in writer._entity_id_map
    assert 17 not in writer._metadata_id_map
    assert 42 not in writer._metadata_id_map


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
async def test_rename_no_pool_is_noop(hass, writer, mock_db_connection):
    """Without a pool the call returns immediately."""
    writer._pool = None
    await writer.rename_entity("sensor.old", "sensor.new")
    mock_db_connection.execute.assert_not_called()
