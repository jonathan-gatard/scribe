"""End-to-end rename tests against a real TimescaleDB.

Unlike test_rename_entity.py (which mocks asyncpg and asserts on SQL strings),
these drive the real writer against a real database and a real Home Assistant
entity registry, then assert on what actually landed in the tables.

Requires a reachable TimescaleDB; skipped otherwise. Point SCRIBE_TEST_DSN at
one, or start the default target with:

    docker run -d --name scribe-test-db -e POSTGRES_PASSWORD=scribe \
        -e POSTGRES_DB=scribe -p 55432:5432 timescale/timescaledb:latest-pg17
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.scribe.writer import ScribeWriter

DSN = os.environ.get(
    "SCRIBE_TEST_DSN", "postgresql://postgres:scribe@127.0.0.1:55432/scribe"
)


async def _dsn_reachable() -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(DSN), timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture(autouse=True)
def mock_create_pool():
    """Neutralize conftest's autouse asyncpg patch — we want the real thing."""
    yield None


@pytest.fixture
async def clean_db(socket_enabled):
    """Wipe Scribe's tables before each test.

    `socket_enabled` lifts the network ban that pytest-homeassistant-custom-component
    installs via pytest-socket; without it every connection attempt is blocked.
    """
    if not await _dsn_reachable():
        pytest.skip(f"no TimescaleDB at {DSN}")
    conn = await asyncpg.connect(DSN)
    for stmt in (
        "DROP VIEW IF EXISTS states CASCADE",
        "DROP TABLE IF EXISTS states_raw CASCADE",
        "DROP TABLE IF EXISTS entities CASCADE",
        "DROP TABLE IF EXISTS events CASCADE",
    ):
        await conn.execute(stmt)
    await conn.close()
    yield


@pytest.fixture
async def writer(hass, clean_db):
    """A fully started writer — its own pool, codecs, schema and hypertables."""
    w = ScribeWriter(
        hass=hass,
        db_url=DSN,
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=100,
        flush_interval=3600,  # never fires on its own; tests flush explicitly
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert=None,
        ssl_cert_file=None,
        ssl_key_file=None,
    )
    await w.start()
    assert w._pool is not None, "writer failed to connect"
    yield w
    await w.stop()


@pytest.fixture
async def db(writer):
    """The writer's own pool, so queries share its jsonb codec."""
    return writer._pool


async def _register(hass, entity_id, unique_id, platform="input_boolean"):
    """Create a real entity registry entry and return it."""
    registry = er.async_get(hass)
    domain, object_id = entity_id.split(".", 1)
    entry = registry.async_get_or_create(
        domain=domain,
        platform=platform,
        unique_id=unique_id,
        suggested_object_id=object_id,
    )
    assert entry.entity_id == entity_id, f"registry gave {entry.entity_id}"
    return entry


async def _sync_metadata(writer, hass, entity_id):
    """Mirror what __init__.handle_entity_registry_update writes to `entities`."""
    entity = er.async_get(hass).async_get(entity_id)
    await writer.write_entities([{
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "platform": entity.platform,
        "domain": entity.domain,
        "name": entity.name or entity.original_name,
        "device_id": entity.device_id,
        "area_id": entity.area_id,
        "capabilities": None,
    }])


BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _write_states(writer, entity_id, count, start=0):
    """Enqueue and flush `count` states, one per second from BASE_TIME+start.

    `start` separates entities in time; passing the same value to two entities
    makes their rows collide on states_raw's (metadata_id, time) primary key.
    """
    for i in range(count):
        writer._queue.append({
            "type": "state",
            "time": BASE_TIME + timedelta(seconds=start + i),
            "entity_id": entity_id,
            "state": f"s{i}",
            "value": float(i),
            "attributes": {},
        })
    await writer._flush()


async def _rows(pool, entity_id):
    """Return (entities.id, number of states_raw rows) for an entity_id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM entities WHERE entity_id = $1", entity_id)
        if row is None:
            return None, 0
        n = await conn.fetchval(
            "SELECT count(*) FROM states_raw WHERE metadata_id = $1", row["id"])
        return row["id"], n


@pytest.mark.asyncio
async def test_e2e_rename_free_target(hass, writer, db):
    """Plain rename: same row, same id, history follows the new name."""
    await _register(hass, "input_boolean.scribe_test", "uid-test")
    await _sync_metadata(writer, hass, "input_boolean.scribe_test")
    await _write_states(writer, "input_boolean.scribe_test", 5)
    original_id, n = await _rows(db, "input_boolean.scribe_test")
    assert n == 5

    await writer.rename_entity(
        "input_boolean.scribe_test", "input_boolean.scribe_test_bis")

    new_id, n_after = await _rows(db, "input_boolean.scribe_test_bis")
    assert new_id == original_id
    assert n_after == 5
    assert (await _rows(db, "input_boolean.scribe_test"))[0] is None


@pytest.mark.asyncio
async def test_e2e_dead_orphan_is_merged(hass, writer, db):
    """Destination held by a removed entity: histories merge into one row."""
    # Victim: recorded, then removed from HA (its `entities` row survives).
    await _register(hass, "input_boolean.scribe_victim", "uid-victim")
    await _sync_metadata(writer, hass, "input_boolean.scribe_victim")
    await _write_states(writer, "input_boolean.scribe_victim", 7, start=0)
    victim_id, victim_rows = await _rows(db, "input_boolean.scribe_victim")
    assert victim_rows == 7
    er.async_get(hass).async_remove("input_boolean.scribe_victim")

    # Phoenix: a fresh entity that will take the victim's name.
    await _register(hass, "input_boolean.scribe_phoenix", "uid-phoenix")
    await _sync_metadata(writer, hass, "input_boolean.scribe_phoenix")
    await _write_states(writer, "input_boolean.scribe_phoenix", 3, start=100)
    phoenix_id, phoenix_rows = await _rows(db, "input_boolean.scribe_phoenix")
    assert phoenix_rows == 3

    await writer.rename_entity(
        "input_boolean.scribe_phoenix", "input_boolean.scribe_victim")

    final_id, final_rows = await _rows(db, "input_boolean.scribe_victim")
    assert final_id == phoenix_id
    assert final_rows == 10  # 7 merged + 3 own: one continuous history
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM entities WHERE id = $1", victim_id) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM entities "
            "WHERE entity_id LIKE 'input_boolean.scribe%'") == 1


@pytest.mark.asyncio
async def test_e2e_merge_with_colliding_timestamps(hass, writer, db):
    """Both entities hold a state at the same instant: merge must not abort.

    states_raw's primary key is (metadata_id, time), so moving the occupant's
    rows onto the survivor's metadata_id violates it wherever the two recorded
    at the same timestamp. Before the fix this raised UniqueViolationError and
    rolled the whole rename back, silently losing it.
    """
    await _register(hass, "input_boolean.scribe_victim", "uid-victim")
    await _sync_metadata(writer, hass, "input_boolean.scribe_victim")
    await _write_states(writer, "input_boolean.scribe_victim", 7, start=0)
    er.async_get(hass).async_remove("input_boolean.scribe_victim")

    await _register(hass, "input_boolean.scribe_phoenix", "uid-phoenix")
    await _sync_metadata(writer, hass, "input_boolean.scribe_phoenix")
    # Deliberate full overlap: same 5 timestamps as the victim's first 5 rows.
    await _write_states(writer, "input_boolean.scribe_phoenix", 5, start=0)
    phoenix_id, _ = await _rows(db, "input_boolean.scribe_phoenix")

    await writer.rename_entity(
        "input_boolean.scribe_phoenix", "input_boolean.scribe_victim")

    final_id, final_rows = await _rows(db, "input_boolean.scribe_victim")
    assert final_id == phoenix_id, "rename was rolled back"
    # 5 own rows kept + victim's 2 non-colliding rows; 5 duplicates dropped.
    assert final_rows == 7
    async with db.acquire() as conn:
        # The survivor's own values won at the shared timestamps.
        state = await conn.fetchval(
            "SELECT state FROM states_raw WHERE metadata_id = $1 AND time = $2",
            final_id, BASE_TIME)
        assert state == "s0"
        assert await conn.fetchval(
            "SELECT count(*) FROM entities "
            "WHERE entity_id LIKE 'input_boolean.scribe%'") == 1


@pytest.mark.asyncio
async def test_e2e_live_occupant_is_refused(hass, writer, db):
    """Destination held by a different *live* entity: nothing is modified."""
    await _register(hass, "input_boolean.scribe_a", "uid-a")
    await _sync_metadata(writer, hass, "input_boolean.scribe_a")
    await _write_states(writer, "input_boolean.scribe_a", 4, start=0)
    await _register(hass, "input_boolean.scribe_b", "uid-b")
    await _sync_metadata(writer, hass, "input_boolean.scribe_b")
    await _write_states(writer, "input_boolean.scribe_b", 6, start=100)
    a_id, _ = await _rows(db, "input_boolean.scribe_a")
    b_id, _ = await _rows(db, "input_boolean.scribe_b")

    await writer.rename_entity("input_boolean.scribe_a", "input_boolean.scribe_b")

    assert await _rows(db, "input_boolean.scribe_a") == (a_id, 4)
    assert await _rows(db, "input_boolean.scribe_b") == (b_id, 6)


@pytest.mark.asyncio
async def test_e2e_self_collision_is_merged(hass, writer, db):
    """The 3.7.0b1 field bug: metadata sync lands the destination row first.

    Reproduces the real sequence — HA renames the entity, the registry-sync
    task writes the *new* entity_id into `entities` before rename_entity runs,
    so the rename collides with the entity's own fresh row.
    """
    await _register(hass, "input_boolean.scribe_test", "uid-test")
    await _sync_metadata(writer, hass, "input_boolean.scribe_test")
    await _write_states(writer, "input_boolean.scribe_test", 5)
    old_id, _ = await _rows(db, "input_boolean.scribe_test")

    # HA performs the rename; the registry now knows the new id.
    er.async_get(hass).async_update_entity(
        "input_boolean.scribe_test", new_entity_id="input_boolean.scribe_test_bis")
    # The sync task wins the race and inserts the destination row.
    await _sync_metadata(writer, hass, "input_boolean.scribe_test_bis")
    await _write_states(writer, "input_boolean.scribe_test_bis", 2, start=100)
    intruder_id, intruder_rows = await _rows(db, "input_boolean.scribe_test_bis")
    assert intruder_id != old_id and intruder_rows == 2

    # Only now does the rename run — straight into a self-collision.
    await writer.rename_entity(
        "input_boolean.scribe_test", "input_boolean.scribe_test_bis")

    final_id, final_rows = await _rows(db, "input_boolean.scribe_test_bis")
    assert final_id == old_id
    assert final_rows == 7  # 5 pre-rename + 2 post-rename, reunited
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM entities "
            "WHERE entity_id LIKE 'input_boolean.scribe%'") == 1


@pytest.mark.asyncio
async def test_e2e_concurrent_rename_and_sync(hass, writer, db):
    """The race itself: rename and metadata sync fired concurrently.

    This is what Home Assistant does — both handlers run as tasks. The
    metadata lock must serialize them into one coherent outcome.
    """
    await _register(hass, "input_boolean.scribe_race", "uid-race")
    await _sync_metadata(writer, hass, "input_boolean.scribe_race")
    await _write_states(writer, "input_boolean.scribe_race", 5)
    old_id, _ = await _rows(db, "input_boolean.scribe_race")

    er.async_get(hass).async_update_entity(
        "input_boolean.scribe_race", new_entity_id="input_boolean.scribe_race2")

    await asyncio.gather(
        writer.rename_entity(
            "input_boolean.scribe_race", "input_boolean.scribe_race2"),
        _sync_metadata(writer, hass, "input_boolean.scribe_race2"),
    )

    # Exactly one row survives, carrying the full history.
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM entities "
            "WHERE entity_id LIKE 'input_boolean.scribe%'") == 1
    final_id, final_rows = await _rows(db, "input_boolean.scribe_race2")
    assert final_id == old_id
    assert final_rows == 5
