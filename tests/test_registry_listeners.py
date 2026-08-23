"""The listeners that keep the metadata tables current.

Home Assistant fires these as it changes its registries. They must mirror the
change into the database, ignore the actions that are not a change, and never
let a database error escape into the event bus — a raising listener would be
logged by Home Assistant and, worse, could take the rest of the callback chain
with it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import Event
from homeassistant.exceptions import HomeAssistantError

from custom_components.scribe import (
    _make_entity_registry_listener,
    _make_registry_listener,
    _make_user_listener,
    _register_services,
)
from custom_components.scribe.const import DOMAIN


def _event(data):
    event = MagicMock(spec=Event)
    event.data = data
    event.event_type = "user_updated"
    return event


@pytest.fixture
def writer():
    w = MagicMock()
    w.write_entities = AsyncMock()
    w.write_users = AsyncMock()
    w.write_devices = AsyncMock()
    w.rename_entity = AsyncMock()
    return w


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rename_reaches_the_database_as_a_rename(hass, writer):
    """Writing the new row first would split the entity's history in two."""
    listener = _make_entity_registry_listener(hass, writer)

    await listener(
        _event(
            {
                "action": "update",
                "entity_id": "sensor.new",
                "old_entity_id": "sensor.old",
            }
        )
    )

    writer.rename_entity.assert_awaited_once_with("sensor.old", "sensor.new")


@pytest.mark.asyncio
async def test_an_update_without_a_rename_is_not_a_rename(hass, writer):
    """`old_entity_id` is present on every update, equal when nothing moved."""
    listener = _make_entity_registry_listener(hass, writer)

    await listener(
        _event(
            {
                "action": "update",
                "entity_id": "sensor.same",
                "old_entity_id": "sensor.same",
            }
        )
    )

    writer.rename_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_removal_writes_nothing(hass, writer):
    """Removing an entity from the registry must not resurrect its row."""
    listener = _make_entity_registry_listener(hass, writer)

    await listener(_event({"action": "remove", "entity_id": "sensor.gone"}))

    writer.write_entities.assert_not_awaited()
    writer.rename_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_entity_that_vanished_before_the_lookup_is_skipped(hass, writer):
    """The registry is read after the event, so the entity may already be gone."""
    listener = _make_entity_registry_listener(hass, writer)

    with patch("custom_components.scribe.er.async_get") as registry:
        registry.return_value.async_get.return_value = None
        await listener(_event({"action": "create", "entity_id": "sensor.ghost"}))

    writer.write_entities.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_database_error_never_escapes_the_listener(hass, writer, caplog):
    """A raising callback would be dropped on the bus, taking the sync with it."""
    writer.write_entities.side_effect = Exception("connection reset")
    listener = _make_entity_registry_listener(hass, writer)

    with patch("custom_components.scribe.er.async_get") as registry:
        registry.return_value.async_get.return_value = MagicMock(
            entity_id="sensor.x",
            unique_id="u",
            platform="demo",
            domain="sensor",
            name="X",
            original_name=None,
            device_id=None,
            area_id=None,
            capabilities=None,
        )
        await listener(_event({"action": "create", "entity_id": "sensor.x"}))

    assert "connection reset" in caplog.text


# ---------------------------------------------------------------------------
# Areas and devices, which share one listener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registry_row_is_mirrored(hass, writer):
    write = AsyncMock()
    listener = _make_registry_listener(
        hass,
        label="handle_device_registry_update",
        id_key="device_id",
        lookup=lambda h, i: {"id": i},
        build_row=lambda obj: {"device_id": obj["id"]},
        write=write,
    )

    await listener(_event({"action": "create", "device_id": "d1"}))

    write.assert_awaited_once_with([{"device_id": "d1"}])


@pytest.mark.asyncio
async def test_a_registry_removal_writes_nothing(hass, writer):
    write = AsyncMock()
    listener = _make_registry_listener(
        hass,
        label="handle_area_registry_update",
        id_key="area_id",
        lookup=lambda h, i: {"id": i},
        build_row=lambda obj: {"area_id": obj["id"]},
        write=write,
    )

    await listener(_event({"action": "remove", "area_id": "a1"}))

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_registry_lookup_failure_is_contained(hass, caplog):
    def explode(_hass, _id):
        raise RuntimeError("registry unavailable")

    listener = _make_registry_listener(
        hass,
        label="handle_device_registry_update",
        id_key="device_id",
        lookup=explode,
        build_row=lambda obj: obj,
        write=AsyncMock(),
    )

    await listener(_event({"action": "update", "device_id": "d1"}))

    assert "registry unavailable" in caplog.text


# ---------------------------------------------------------------------------
# Users, which are not a registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_change_is_mirrored(hass, writer):
    listener = _make_user_listener(hass, writer)
    user = MagicMock(
        id="u1", name="Alice", is_owner=True, is_active=True, system_generated=False
    )
    user.groups = []

    with patch.object(hass.auth, "async_get_user", AsyncMock(return_value=user)):
        await listener(_event({"user_id": "u1"}))

    writer.write_users.assert_awaited_once()
    assert writer.write_users.await_args.args[0][0]["user_id"] == "u1"


@pytest.mark.asyncio
async def test_a_removed_user_writes_nothing(hass, writer):
    """user_removed fires after the user is gone, so the lookup returns None."""
    listener = _make_user_listener(hass, writer)

    with patch.object(hass.auth, "async_get_user", AsyncMock(return_value=None)):
        await listener(_event({"user_id": "gone"}))

    writer.write_users.assert_not_awaited()


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_service_requires_some_sql(hass, writer):
    _register_services(hass, writer)
    writer.query = AsyncMock()

    with pytest.raises(HomeAssistantError, match="required"):
        await hass.services.async_call(
            DOMAIN, "query", {"sql": ""}, blocking=True, return_response=True
        )


@pytest.mark.asyncio
async def test_the_query_service_reports_a_rejected_query(hass, writer):
    """A validation refusal must reach the caller, not just the log."""
    _register_services(hass, writer)
    writer.query = AsyncMock(side_effect=ValueError("only SELECT is allowed"))

    with pytest.raises(HomeAssistantError, match="only SELECT"):
        await hass.services.async_call(
            DOMAIN,
            "query",
            {"sql": "DELETE FROM states_raw"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_the_query_service_reports_a_database_error(hass, writer):
    _register_services(hass, writer)
    writer.query = AsyncMock(side_effect=Exception("relation does not exist"))

    with pytest.raises(HomeAssistantError, match="relation does not exist"):
        await hass.services.async_call(
            DOMAIN,
            "query",
            {"sql": "SELECT * FROM nope"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.asyncio
async def test_the_flush_service_flushes(hass, writer):
    _register_services(hass, writer)
    writer._flush = AsyncMock()

    await hass.services.async_call(DOMAIN, "flush", {}, blocking=True)

    writer._flush.assert_awaited_once()
