"""End-to-end entity rename tests against a real TimescaleDB.

The mocked counterpart is tests/test_rename_entity.py, which asserts on the
SQL issued; these assert on what actually ends up in the tables. Fixtures and
helpers live in conftest.py.
"""

import asyncio

import pytest

from homeassistant.helpers import entity_registry as er

from .conftest import (
    BASE_TIME,
    entity_rows,
    register_entity,
    sync_metadata,
    write_states,
)


@pytest.mark.asyncio
async def test_e2e_rename_free_target(hass, writer, db):
    """Plain rename: same row, same id, history follows the new name."""
    await register_entity(hass, "input_boolean.scribe_test", "uid-test")
    await sync_metadata(writer, hass, "input_boolean.scribe_test")
    await write_states(writer, "input_boolean.scribe_test", 5)
    original_id, n = await entity_rows(db, "input_boolean.scribe_test")
    assert n == 5

    await writer.rename_entity(
        "input_boolean.scribe_test", "input_boolean.scribe_test_bis"
    )

    new_id, n_after = await entity_rows(db, "input_boolean.scribe_test_bis")
    assert new_id == original_id
    assert n_after == 5
    assert (await entity_rows(db, "input_boolean.scribe_test"))[0] is None


@pytest.mark.asyncio
async def test_e2e_dead_orphan_is_merged(hass, writer, db):
    """Destination held by a removed entity: histories merge into one row."""
    # Victim: recorded, then removed from HA (its `entities` row survives).
    await register_entity(hass, "input_boolean.scribe_victim", "uid-victim")
    await sync_metadata(writer, hass, "input_boolean.scribe_victim")
    await write_states(writer, "input_boolean.scribe_victim", 7, start=0)
    victim_id, victim_rows = await entity_rows(db, "input_boolean.scribe_victim")
    assert victim_rows == 7
    er.async_get(hass).async_remove("input_boolean.scribe_victim")

    # Phoenix: a fresh entity that will take the victim's name.
    await register_entity(hass, "input_boolean.scribe_phoenix", "uid-phoenix")
    await sync_metadata(writer, hass, "input_boolean.scribe_phoenix")
    await write_states(writer, "input_boolean.scribe_phoenix", 3, start=100)
    phoenix_id, phoenix_rows = await entity_rows(db, "input_boolean.scribe_phoenix")
    assert phoenix_rows == 3

    await writer.rename_entity(
        "input_boolean.scribe_phoenix", "input_boolean.scribe_victim"
    )

    final_id, final_rows = await entity_rows(db, "input_boolean.scribe_victim")
    assert final_id == phoenix_id
    assert final_rows == 10  # 7 merged + 3 own: one continuous history
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities WHERE id = $1", victim_id
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities "
                "WHERE entity_id LIKE 'input_boolean.scribe%'"
            )
            == 1
        )


@pytest.mark.asyncio
async def test_e2e_merge_with_colliding_timestamps(hass, writer, db):
    """Both entities hold a state at the same instant: merge must not abort.

    states_raw's primary key is (metadata_id, time), so moving the occupant's
    rows onto the survivor's metadata_id violates it wherever the two recorded
    at the same timestamp. Before the fix this raised UniqueViolationError and
    rolled the whole rename back, silently losing it.
    """
    await register_entity(hass, "input_boolean.scribe_victim", "uid-victim")
    await sync_metadata(writer, hass, "input_boolean.scribe_victim")
    await write_states(writer, "input_boolean.scribe_victim", 7, start=0)
    er.async_get(hass).async_remove("input_boolean.scribe_victim")

    await register_entity(hass, "input_boolean.scribe_phoenix", "uid-phoenix")
    await sync_metadata(writer, hass, "input_boolean.scribe_phoenix")
    # Deliberate full overlap: same 5 timestamps as the victim's first 5 rows.
    await write_states(writer, "input_boolean.scribe_phoenix", 5, start=0)
    phoenix_id, _ = await entity_rows(db, "input_boolean.scribe_phoenix")

    await writer.rename_entity(
        "input_boolean.scribe_phoenix", "input_boolean.scribe_victim"
    )

    final_id, final_rows = await entity_rows(db, "input_boolean.scribe_victim")
    assert final_id == phoenix_id, "rename was rolled back"
    # 5 own rows kept + victim's 2 non-colliding rows; 5 duplicates dropped.
    assert final_rows == 7
    async with db.acquire() as conn:
        # The survivor's own values won at the shared timestamps.
        state = await conn.fetchval(
            "SELECT state FROM states_raw WHERE metadata_id = $1 AND time = $2",
            final_id,
            BASE_TIME,
        )
        assert state == "s0"
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities "
                "WHERE entity_id LIKE 'input_boolean.scribe%'"
            )
            == 1
        )


@pytest.mark.asyncio
async def test_e2e_live_occupant_is_refused(hass, writer, db):
    """Destination held by a different *live* entity: nothing is modified."""
    await register_entity(hass, "input_boolean.scribe_a", "uid-a")
    await sync_metadata(writer, hass, "input_boolean.scribe_a")
    await write_states(writer, "input_boolean.scribe_a", 4, start=0)
    await register_entity(hass, "input_boolean.scribe_b", "uid-b")
    await sync_metadata(writer, hass, "input_boolean.scribe_b")
    await write_states(writer, "input_boolean.scribe_b", 6, start=100)
    a_id, _ = await entity_rows(db, "input_boolean.scribe_a")
    b_id, _ = await entity_rows(db, "input_boolean.scribe_b")

    await writer.rename_entity("input_boolean.scribe_a", "input_boolean.scribe_b")

    assert await entity_rows(db, "input_boolean.scribe_a") == (a_id, 4)
    assert await entity_rows(db, "input_boolean.scribe_b") == (b_id, 6)


@pytest.mark.asyncio
async def test_e2e_self_collision_is_merged(hass, writer, db):
    """The 3.7.0b1 field bug: metadata sync lands the destination row first.

    Reproduces the real sequence — HA renames the entity, the registry-sync
    task writes the *new* entity_id into `entities` before rename_entity runs,
    so the rename collides with the entity's own fresh row.
    """
    await register_entity(hass, "input_boolean.scribe_test", "uid-test")
    await sync_metadata(writer, hass, "input_boolean.scribe_test")
    await write_states(writer, "input_boolean.scribe_test", 5)
    old_id, _ = await entity_rows(db, "input_boolean.scribe_test")

    # HA performs the rename; the registry now knows the new id.
    er.async_get(hass).async_update_entity(
        "input_boolean.scribe_test", new_entity_id="input_boolean.scribe_test_bis"
    )
    # The sync task wins the race and inserts the destination row.
    await sync_metadata(writer, hass, "input_boolean.scribe_test_bis")
    await write_states(writer, "input_boolean.scribe_test_bis", 2, start=100)
    intruder_id, intruder_rows = await entity_rows(db, "input_boolean.scribe_test_bis")
    assert intruder_id != old_id and intruder_rows == 2

    # Only now does the rename run — straight into a self-collision.
    await writer.rename_entity(
        "input_boolean.scribe_test", "input_boolean.scribe_test_bis"
    )

    final_id, final_rows = await entity_rows(db, "input_boolean.scribe_test_bis")
    assert final_id == old_id
    assert final_rows == 7  # 5 pre-rename + 2 post-rename, reunited
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities "
                "WHERE entity_id LIKE 'input_boolean.scribe%'"
            )
            == 1
        )


@pytest.mark.asyncio
async def test_e2e_concurrent_rename_and_sync(hass, writer, db):
    """The race itself: rename and metadata sync fired concurrently.

    This is what Home Assistant does — both handlers run as tasks. The
    metadata lock must serialize them into one coherent outcome.
    """
    await register_entity(hass, "input_boolean.scribe_race", "uid-race")
    await sync_metadata(writer, hass, "input_boolean.scribe_race")
    await write_states(writer, "input_boolean.scribe_race", 5)
    old_id, _ = await entity_rows(db, "input_boolean.scribe_race")

    er.async_get(hass).async_update_entity(
        "input_boolean.scribe_race", new_entity_id="input_boolean.scribe_race2"
    )

    await asyncio.gather(
        writer.rename_entity("input_boolean.scribe_race", "input_boolean.scribe_race2"),
        sync_metadata(writer, hass, "input_boolean.scribe_race2"),
    )

    # Exactly one row survives, carrying the full history.
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities "
                "WHERE entity_id LIKE 'input_boolean.scribe%'"
            )
            == 1
        )
    final_id, final_rows = await entity_rows(db, "input_boolean.scribe_race2")
    assert final_id == old_id
    assert final_rows == 5
