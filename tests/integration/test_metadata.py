"""End-to-end metadata sync: entities, users, areas, devices, integrations."""

import pytest

from .conftest import register_entity, sync_metadata


def _entity(entity_id="sensor.meta", **overrides):
    row = {
        "entity_id": entity_id,
        "unique_id": "uid-1",
        "platform": "mqtt",
        "domain": "sensor",
        "name": "Meta Sensor",
        "device_id": "dev-1",
        "area_id": "area-1",
        "capabilities": {"options": ["a", "b"]},
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_entities_insert_and_update(writer, db):
    """First write inserts; a changed field updates the same row in place."""
    await writer.write_entities([_entity()])
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM entities WHERE entity_id = 'sensor.meta'"
        )
    original_id = row["id"]
    assert row["unique_id"] == "uid-1"
    assert row["capabilities"] == {"options": ["a", "b"]}

    await writer.write_entities([_entity(name="Renamed", area_id="area-2")])
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM entities WHERE entity_id = 'sensor.meta'"
        )
        count = await conn.fetchval("SELECT count(*) FROM entities")
    assert row["id"] == original_id, "update must not create a new row"
    assert row["name"] == "Renamed"
    assert row["area_id"] == "area-2"
    assert count == 1


@pytest.mark.asyncio
async def test_unchanged_entities_do_not_burn_sequence_ids(writer, db):
    """Re-syncing identical rows must not advance the SERIAL sequence.

    The writer deliberately avoids `INSERT ... ON CONFLICT DO UPDATE`, which
    consumes an id per conflicting row and balloons the sequence on every full
    registry resync.
    """
    await writer.write_entities([_entity()])
    async with db.acquire() as conn:
        seq_before = await conn.fetchval("SELECT last_value FROM entities_id_seq")

    for _ in range(5):
        await writer.write_entities([_entity()])

    async with db.acquire() as conn:
        seq_after = await conn.fetchval("SELECT last_value FROM entities_id_seq")
        assert await conn.fetchval("SELECT count(*) FROM entities") == 1
    assert seq_after == seq_before


@pytest.mark.asyncio
async def test_entities_sync_from_a_real_registry(writer, hass, db):
    """The shape __init__ builds from a registry entry stores correctly."""
    await register_entity(hass, "sensor.from_registry", "uid-registry", platform="mqtt")
    await sync_metadata(writer, hass, "sensor.from_registry")

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM entities WHERE entity_id = 'sensor.from_registry'"
        )
    assert row["unique_id"] == "uid-registry"
    assert row["platform"] == "mqtt"
    assert row["domain"] == "sensor"


@pytest.mark.asyncio
async def test_entities_batch_mixes_inserts_and_updates(writer, db):
    """A batch containing new and existing entities handles both correctly."""
    await writer.write_entities([_entity("sensor.a", unique_id="uid-a")])
    await writer.write_entities(
        [
            _entity("sensor.a", unique_id="uid-a", name="A updated"),
            _entity("sensor.b", unique_id="uid-b"),
        ]
    )

    async with db.acquire() as conn:
        rows = {
            r["entity_id"]: r
            for r in await conn.fetch(
                "SELECT entity_id, name FROM entities ORDER BY entity_id"
            )
        }
    assert rows["sensor.a"]["name"] == "A updated"
    assert rows["sensor.b"]["name"] == "Meta Sensor"


@pytest.mark.asyncio
async def test_null_bytes_stripped_from_entity_metadata(writer, db):
    """Text fields are cleaned before they reach Postgres."""
    await writer.write_entities([_entity(name="na\0me")])
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT name FROM entities WHERE entity_id = 'sensor.meta'"
            )
            == "name"
        )


@pytest.mark.asyncio
async def test_users_upsert(writer, db):
    """Users are inserted then updated in place, keyed by user_id."""
    await writer.write_users(
        [
            {
                "user_id": "u1",
                "name": "Jonathan",
                "is_owner": True,
                "is_active": True,
                "system_generated": False,
                "group_ids": ["admin"],
            }
        ]
    )
    await writer.write_users(
        [
            {
                "user_id": "u1",
                "name": "Jonathan G",
                "is_owner": True,
                "is_active": False,
                "system_generated": False,
                "group_ids": ["admin", "users"],
            }
        ]
    )

    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users")
    assert len(rows) == 1
    assert rows[0]["name"] == "Jonathan G"
    assert rows[0]["is_active"] is False
    assert rows[0]["group_ids"] == ["admin", "users"]


@pytest.mark.asyncio
async def test_areas_upsert(writer, db):
    """Areas are keyed by area_id."""
    await writer.write_areas([{"area_id": "a1", "name": "Salon", "picture": None}])
    await writer.write_areas([{"area_id": "a1", "name": "Living", "picture": "/p.png"}])

    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM areas")
    assert len(rows) == 1
    assert rows[0]["name"] == "Living"
    assert rows[0]["picture"] == "/p.png"


@pytest.mark.asyncio
async def test_devices_upsert(writer, db):
    """Devices are keyed by device_id."""
    device = {
        "device_id": "d1",
        "name": "Thermostat",
        "name_by_user": None,
        "model": "T1",
        "manufacturer": "Acme",
        "sw_version": "1.0",
        "area_id": "a1",
        "primary_config_entry": "entry-1",
    }
    await writer.write_devices([device])
    await writer.write_devices(
        [{**device, "sw_version": "2.0", "name_by_user": "Chaudière"}]
    )

    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM devices")
    assert len(rows) == 1
    assert rows[0]["sw_version"] == "2.0"
    assert rows[0]["name_by_user"] == "Chaudière"


@pytest.mark.asyncio
async def test_integrations_upsert(writer, db):
    """Integrations are keyed by entry_id."""
    entry = {
        "entry_id": "e1",
        "domain": "mqtt",
        "title": "MQTT",
        "state": "loaded",
        "source": "user",
    }
    await writer.write_integrations([entry])
    await writer.write_integrations([{**entry, "state": "setup_error"}])

    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM integrations")
    assert len(rows) == 1
    assert rows[0]["state"] == "setup_error"


@pytest.mark.asyncio
async def test_empty_metadata_batches_are_noops(writer, db):
    """Passing an empty list must not raise or write anything."""
    await writer.write_entities([])
    await writer.write_users([])
    await writer.write_areas([])
    await writer.write_devices([])
    await writer.write_integrations([])

    async with db.acquire() as conn:
        for table in ("entities", "users", "areas", "devices", "integrations"):
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
