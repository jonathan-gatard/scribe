"""Scribe: A custom component to store Home Assistant history in TimescaleDB.

This component intercepts all state changes and events in Home Assistant and asynchronously
writes them to a TimescaleDB (PostgreSQL) database. It uses a dedicated writer task
to ensure that database operations do not block the main Home Assistant event loop.
"""

import logging
from dataclasses import dataclass
from inspect import isawaitable
from collections.abc import Callable

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import (
    config_validation as cv,
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
)
from homeassistant.helpers.entityfilter import generate_filter

from .const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_DB_SSL,
    CONF_SSL_ROOT_CERT,
    CONF_SSL_CERT_FILE,
    CONF_SSL_KEY_FILE,
    CONF_CHUNK_TIME_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_RETENTION_STATES,
    CONF_RETENTION_EVENTS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_INCLUDE_EVENTS,
    CONF_EXCLUDE_EVENTS,
    CONF_RECORD_STATES,
    CONF_RECORD_EVENTS,
    CONF_BATCH_SIZE,
    CONF_FLUSH_INTERVAL,
    CONF_MAX_QUEUE_SIZE,
    CONF_ENABLE_STATS_IO,
    CONF_ENABLE_STATS_CHUNK,
    CONF_ENABLE_STATS_SIZE,
    CONF_STATS_CHUNK_INTERVAL,
    CONF_STATS_SIZE_INTERVAL,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RETENTION_STATES,
    DEFAULT_RETENTION_EVENTS,
    DEFAULT_DB_SSL,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_ENABLE_STATS_IO,
    DEFAULT_ENABLE_STATS_CHUNK,
    DEFAULT_ENABLE_STATS_SIZE,
    DEFAULT_STATS_CHUNK_INTERVAL,
    DEFAULT_STATS_SIZE_INTERVAL,
    CONF_BUFFER_ON_FAILURE,
    DEFAULT_BUFFER_ON_FAILURE,
    CONF_ENABLE_AREAS,
    DEFAULT_ENABLE_AREAS,
    CONF_ENABLE_DEVICES,
    DEFAULT_ENABLE_DEVICES,
    CONF_ENABLE_INTEGRATIONS,
    DEFAULT_ENABLE_INTEGRATIONS,
    CONF_ENABLE_USERS,
    DEFAULT_ENABLE_USERS,
)
from .writer import ScribeWriter, WriterConfig

_LOGGER = logging.getLogger(__name__)

# Configuration Schema for YAML configuration
# This allows users to configure Scribe via configuration.yaml instead of the UI.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DB_URL): cv.string,
                vol.Optional(CONF_DB_SSL): cv.boolean,
                vol.Optional(CONF_SSL_ROOT_CERT): cv.string,
                vol.Optional(CONF_SSL_CERT_FILE): cv.string,
                vol.Optional(CONF_SSL_KEY_FILE): cv.string,
                vol.Optional(CONF_CHUNK_TIME_INTERVAL): cv.string,
                vol.Optional(CONF_COMPRESS_AFTER): cv.string,
                vol.Optional(CONF_RETENTION_STATES): cv.string,
                vol.Optional(CONF_RETENTION_EVENTS): cv.string,
                vol.Optional(CONF_RECORD_STATES): cv.boolean,
                vol.Optional(CONF_RECORD_EVENTS): cv.boolean,
                vol.Optional(CONF_BATCH_SIZE): cv.positive_int,
                vol.Optional(CONF_FLUSH_INTERVAL): cv.positive_int,
                vol.Optional(CONF_MAX_QUEUE_SIZE): cv.positive_int,
                vol.Optional(CONF_BUFFER_ON_FAILURE): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_IO): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_CHUNK): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_SIZE): cv.boolean,
                vol.Optional(CONF_STATS_CHUNK_INTERVAL): cv.positive_int,
                vol.Optional(CONF_STATS_SIZE_INTERVAL): cv.positive_int,
                vol.Optional(CONF_INCLUDE_DOMAINS): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_INCLUDE_ENTITIES): vol.All(
                    cv.ensure_list, [cv.entity_id]
                ),
                vol.Optional(CONF_INCLUDE_ENTITY_GLOBS): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_EXCLUDE_DOMAINS): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_EXCLUDE_ENTITIES): vol.All(
                    cv.ensure_list, [cv.entity_id]
                ),
                vol.Optional(CONF_EXCLUDE_ENTITY_GLOBS): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_EXCLUDE_ATTRIBUTES): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_INCLUDE_EVENTS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_EXCLUDE_EVENTS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_ENABLE_AREAS): cv.boolean,
                vol.Optional(CONF_ENABLE_DEVICES): cv.boolean,
                vol.Optional(CONF_ENABLE_INTEGRATIONS): cv.boolean,
                vol.Optional(CONF_ENABLE_USERS): cv.boolean,
            },
            extra=vol.ALLOW_EXTRA,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _build_exclude_priority_filter(
    base_filter,
    exclude_entities,
    exclude_entity_globs,
):
    """Wrap ``base_filter`` so an exclude-glob match always rejects.

    Home Assistant's ``generate_filter`` (case 4a) short-circuits on
    ``include_entity_globs`` — when an entity matches an include glob the
    exclude globs are never checked. Scribe users expect the opposite:
    ``exclude_entity_globs`` should be a hard reject regardless of what
    the include configuration looks like.

    The wrapper checks ``exclude_entities`` and ``exclude_entity_globs``
    first; if either matches, the entity is rejected. Otherwise the call
    falls through to the upstream filter, preserving all other
    Home-Assistant filter semantics (domain include/exclude, the
    no-filter pass-through, etc.).
    """
    import fnmatch

    exclude_entities_set = set(exclude_entities or [])
    glob_patterns = list(exclude_entity_globs or [])

    if not exclude_entities_set and not glob_patterns:
        return base_filter

    def _excluded(entity_id: str) -> bool:
        if entity_id in exclude_entities_set:
            return True
        return any(fnmatch.fnmatchcase(entity_id, pat) for pat in glob_patterns)

    def _filter(entity_id: str) -> bool:
        if _excluded(entity_id):
            return False
        return base_filter(entity_id)

    return _filter


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Scribe component from YAML.

    This function is called when Home Assistant starts and finds a 'scribe:' entry in configuration.yaml.
    It triggers the import flow to create a config entry if one doesn't exist.
    """
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN in config:
        _LOGGER.info(
            "[__init__.async_setup] Scribe configuration found in YAML. Verifying setup..."
        )
        hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )

    return True


def _user_row(user) -> dict:
    """One Home Assistant user, as a row for the `users` table."""
    return {
        "user_id": user.id,
        "name": user.name,
        "is_owner": user.is_owner,
        "is_active": user.is_active,
        "system_generated": user.system_generated,
        "group_ids": [g.id for g in user.groups] if user.groups else [],
    }


def _entity_row(entity) -> dict:
    """One registry entity, as a row for the `entities` table."""
    return {
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "platform": entity.platform,
        "domain": entity.domain,
        "name": entity.name or entity.original_name,
        "device_id": entity.device_id,
        "area_id": entity.area_id,
        "capabilities": entity.capabilities if entity.capabilities else None,
    }


def _area_row(area) -> dict:
    """One area, as a row for the `areas` table."""
    return {"area_id": area.id, "name": area.name, "picture": area.picture}


def _device_row(device) -> dict:
    """One device, as a row for the `devices` table."""
    return {
        "device_id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "area_id": device.area_id,
        "primary_config_entry": next(iter(device.config_entries), None)
        if device.config_entries
        else None,
    }


def _integration_row(entry) -> dict:
    """One config entry, as a row for the `integrations` table."""
    return {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "state": entry.state.value
        if hasattr(entry.state, "value")
        else str(entry.state),
        "source": entry.source,
    }


async def _collect_users(hass) -> list[dict]:
    """Every Home Assistant user."""
    return [_user_row(user) for user in await hass.auth.async_get_users()]


def _collect_entities(hass) -> list[dict]:
    """The whole entity registry."""
    return [_entity_row(e) for e in er.async_get(hass).entities.values()]


def _collect_areas(hass) -> list[dict]:
    """The whole area registry."""
    return [_area_row(a) for a in ar.async_get(hass).areas.values()]


def _collect_devices(hass) -> list[dict]:
    """The whole device registry."""
    return [_device_row(d) for d in dr.async_get(hass).devices.values()]


def _collect_integrations(hass) -> list[dict]:
    """Every loaded config entry."""
    return [_integration_row(e) for e in hass.config_entries.async_entries()]


async def _sync_metadata(hass, writer):
    """Push every enabled registry into its table.

    One table per iteration, each with its own try: a device registry that
    raises must not stop the areas from being synced. `entities` has no toggle —
    every state write resolves through it.
    """
    for enabled, label, collect, write in (
        (writer.enable_table_users, "users", _collect_users, writer.write_users),
        (True, "entities", _collect_entities, writer.write_entities),
        (writer.enable_table_areas, "areas", _collect_areas, writer.write_areas),
        (
            writer.enable_table_devices,
            "devices",
            _collect_devices,
            writer.write_devices,
        ),
        (
            writer.enable_table_integrations,
            "integrations",
            _collect_integrations,
            writer.write_integrations,
        ),
    ):
        if not enabled:
            continue
        try:
            # Only the user list is fetched asynchronously; the registries are
            # already in memory. Awaiting what needs it keeps the table above
            # uniform without making four plain reads pretend to be async.
            rows = collect(hass)
            if isawaitable(rows):
                rows = await rows
            if not rows:
                _LOGGER.debug("[__init__._sync_metadata:%s] Nothing to sync", label)
                continue
            _LOGGER.debug(
                "[__init__._sync_metadata:%s] Syncing %d rows to database",
                label,
                len(rows),
            )
            await write(rows)
        except Exception as e:
            _LOGGER.error(
                "[__init__._sync_metadata:%s] Error syncing: %s (%s)",
                label,
                e,
                type(e).__name__,
                exc_info=True,
            )


async def _refresh_coordinators(*coordinators):
    """Prime the statistics coordinators that were enabled, if any."""
    for coordinator in coordinators:
        if coordinator is None:
            continue
        try:
            await coordinator.async_refresh()
        except Exception as e:
            _LOGGER.error(
                "[__init__._refresh_coordinators] Failed to refresh %s: %s (%s)",
                type(coordinator).__name__,
                e,
                type(e).__name__,
                exc_info=True,
            )


def _make_state_listener(writer, entity_filter, exclude_attributes):
    """Build the state-change callback.

    Must stay a *synchronous* callback: Home Assistant dispatches state changes
    on the event loop, and an async listener would queue a task per state.
    """

    @callback
    def handle_state_event(event: Event):
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if new_state is None:
            return

        if not entity_filter(entity_id):
            _LOGGER.debug("[__init__.handle_event] Entity %s filtered out", entity_id)
            return

        try:
            state_val = float(new_state.state)
            state_str = None
        except (ValueError, TypeError):
            # Not numeric: keep the text and leave `value` NULL.
            state_val = None
            state_str = new_state.state

        try:
            writer.enqueue(
                {
                    "type": "state",
                    "time": new_state.last_updated,
                    "entity_id": entity_id,
                    "state": state_str,
                    "value": state_val,
                    "attributes": {
                        k: v
                        for k, v in new_state.attributes.items()
                        if k not in exclude_attributes
                    },
                }
            )
        except Exception as e:
            _LOGGER.error(
                "[__init__.handle_event] Error enqueuing state for %s (state=%r): %s (%s)",
                entity_id,
                new_state.state if new_state else None,
                e,
                type(e).__name__,
                exc_info=True,
            )

    return handle_state_event


def _make_event_listener(writer, exclude_events):
    """Build the callback for everything that is not a state change."""
    seen = {"total": 0}

    @callback
    def handle_other_events(event: Event):
        if event.event_type == EVENT_STATE_CHANGED:
            return  # already handled by the state listener
        if event.event_type in exclude_events:
            return

        seen["total"] += 1
        if seen["total"] <= 5:
            _LOGGER.debug(
                "[__init__.handle_other_events] First events seen: %s",
                event.event_type,
            )

        try:
            writer.enqueue(
                {
                    "type": "event",
                    "time": event.time_fired,
                    "event_type": event.event_type,
                    "event_data": event.data,
                    "origin": str(event.origin),
                    "context_id": event.context.id,
                    "context_user_id": event.context.user_id,
                    "context_parent_id": event.context.parent_id,
                }
            )
        except Exception as e:
            _LOGGER.error(
                "[__init__.handle_other_events] Error processing event %s: %s (%s)",
                event.event_type,
                e,
                type(e).__name__,
                exc_info=True,
            )

    return handle_other_events


def _register_event_listeners(hass, entry, writer, include_events, exclude_events):
    """Subscribe to events, as narrowly as the configuration allows.

    Naming the wanted types subscribes to each one; without that list the only
    option is the bus-wide MATCH_ALL, which costs a callback per event fired.
    """
    listener = _make_event_listener(writer, exclude_events)

    if include_events:
        _LOGGER.debug(
            "[__init__.async_setup_entry] Registering listeners for specific events: %s",
            include_events,
        )
        for event_type in include_events:
            entry.async_on_unload(hass.bus.async_listen(event_type, listener))
        return

    from homeassistant.const import MATCH_ALL

    _LOGGER.debug(
        "[__init__.async_setup_entry] Registering listener for ALL events (MATCH_ALL)"
    )
    entry.async_on_unload(hass.bus.async_listen(MATCH_ALL, listener))


def _make_entity_registry_listener(hass, writer):
    """Build the entity-registry listener.

    Kept apart from the other registries because of the rename: Home Assistant
    reports it as an `update` carrying `old_entity_id`, and it has to reach the
    database as a rename — writing the new row first would split the history.
    """

    async def handle_entity_registry_update(event: Event):
        action = event.data.get("action")
        entity_id = event.data.get("entity_id")

        if action == "update":
            old_entity_id = event.data.get("old_entity_id")
            if old_entity_id and old_entity_id != entity_id:
                _LOGGER.debug(
                    "[__init__.handle_entity_registry_update] Entity renamed: %s -> %s",
                    old_entity_id,
                    entity_id,
                )
                await writer.rename_entity(old_entity_id, entity_id)

        if action not in ("create", "update"):
            return

        _LOGGER.debug(
            "[__init__.handle_entity_registry_update] Registry update: %s %s",
            action,
            entity_id,
        )
        try:
            entity = er.async_get(hass).async_get(entity_id)
            if entity:
                await writer.write_entities([_entity_row(entity)])
        except Exception as e:
            _LOGGER.error(
                "[__init__.handle_entity_registry_update] Error syncing entity %s (action=%s): %s (%s)",
                entity_id,
                action,
                e,
                type(e).__name__,
                exc_info=True,
            )

    return handle_entity_registry_update


def _make_registry_listener(hass, *, label, id_key, lookup, build_row, write):
    """Build a listener that mirrors one registry row into its table.

    Areas and devices differ only in which registry is read and which row is
    built, so they share this: one place where a create/update reaches the
    database, and one place where a failure is reported.
    """

    async def handle_registry_update(event: Event):
        action = event.data.get("action")
        object_id = event.data.get(id_key)

        if action not in ("create", "update"):
            return

        _LOGGER.debug("[__init__.%s] Registry update: %s %s", label, action, object_id)
        try:
            obj = lookup(hass, object_id)
            if obj:
                await write([build_row(obj)])
        except Exception as e:
            _LOGGER.error(
                "[__init__.%s] Error syncing %s (action=%s): %s (%s)",
                label,
                object_id,
                action,
                e,
                type(e).__name__,
                exc_info=True,
            )

    return handle_registry_update


def _make_user_listener(hass, writer):
    """Build the listener for user_added / user_updated / user_removed.

    Users are not a registry: the event carries no action, and the lookup is
    asynchronous.
    """

    async def handle_user_update(event: Event):
        user_id = event.data.get("user_id")
        _LOGGER.debug(
            "[__init__.handle_user_update] Registry update: %s %s",
            event.event_type,
            user_id,
        )
        try:
            user = await hass.auth.async_get_user(user_id)
            if user:
                await writer.write_users([_user_row(user)])
        except Exception as e:
            _LOGGER.error(
                "[__init__.handle_user_update] Error syncing user %s (action=%s): %s (%s)",
                user_id,
                event.event_type,
                e,
                type(e).__name__,
                exc_info=True,
            )

    return handle_user_update


def _create_coordinators(hass, writer, *, chunk_minutes, size_minutes):
    """Build the statistics coordinators that are enabled.

    A minutes value of None means the matching sensors are off. Each one is
    built in its own try: a coordinator that cannot start must not take the
    other — or the setup — down with it.
    """
    from .coordinator import ScribeDataUpdateCoordinator

    coordinators = []
    for minutes, stats_type in ((chunk_minutes, "chunk"), (size_minutes, "size")):
        if minutes is None:
            coordinators.append(None)
            continue
        try:
            coordinators.append(
                ScribeDataUpdateCoordinator(
                    hass,
                    writer,
                    update_interval_minutes=minutes,
                    stats_type=stats_type,
                )
            )
        except Exception as e:
            _LOGGER.error(
                "[__init__.async_setup_entry] Failed to setup %s coordinator (interval=%s): %s (%s)",
                stats_type,
                minutes,
                e,
                type(e).__name__,
                exc_info=True,
            )
            coordinators.append(None)
    return tuple(coordinators)


def _register_services(hass, writer):
    """Register the `scribe.flush` and `scribe.query` services."""

    async def handle_flush(call):
        """Flush whatever is buffered, now."""
        _LOGGER.info("[__init__.handle_flush] Manual flush triggered via service call")
        await writer._flush()

    async def handle_query(call):
        """Run a read-only SQL query and return its rows."""
        sql = call.data.get("sql")
        if not sql:
            raise HomeAssistantError("SQL query is required")

        try:
            return {"result": await writer.query(sql)}
        except ValueError as e:
            # Rejected before reaching the database (e.g. not a SELECT).
            _LOGGER.warning(
                "[__init__.handle_query] Rejected query (validation): %s", e
            )
            raise HomeAssistantError(str(e))
        except Exception as e:
            sqlstate = getattr(e, "sqlstate", None)
            _LOGGER.error(
                "[__init__.handle_query] Query failed (sqlstate=%s, type=%s): %s",
                sqlstate,
                type(e).__name__,
                e,
                exc_info=True,
            )
            raise HomeAssistantError(f"Query failed: {e} ({type(e).__name__})")

    hass.services.async_register(DOMAIN, "flush", handle_flush)
    hass.services.async_register(
        DOMAIN,
        "query",
        handle_query,
        schema=vol.Schema({vol.Required("sql"): cv.string}),
        supports_response=True,
    )


@dataclass(frozen=True)
class _Settings:
    """Everything setup needs, already resolved from its four sources."""

    writer: WriterConfig
    stats_chunk_minutes: int | None
    stats_size_minutes: int | None
    entity_filter: Callable[[str], bool]
    exclude_attributes: set
    include_events: set
    exclude_events: set

    @property
    def record_states(self) -> bool:
        return self.writer.record_states

    @property
    def record_events(self) -> bool:
        return self.writer.record_events

    @property
    def enable_stats_io(self) -> bool:
        return self.writer.enable_stats_io


def _resolve_db_url(config: dict, yaml_config: dict) -> str | None:
    """Find the connection URL, rebuilding it from the pre-3.x pieces if needed.

    YAML first, like every other setting. The config entry was checked first
    until 4.0, which made `db_url` the one key a `configuration.yaml` edit could
    not change: the entry keeps the URL it was created with, so editing the
    line did nothing and moving Scribe to another database meant deleting the
    integration and setting it up again.
    """
    if CONF_DB_URL in yaml_config:
        return yaml_config[CONF_DB_URL]
    if CONF_DB_URL in config:
        return config[CONF_DB_URL]

    db_user = config.get("db_user") or yaml_config.get("db_user")
    db_pass = config.get("db_password") or yaml_config.get("db_password")
    db_host = config.get("db_host") or yaml_config.get("db_host")
    db_port = config.get("db_port") or yaml_config.get("db_port")
    db_name = config.get("db_name") or yaml_config.get("db_name")
    if db_user and db_host:
        return f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

    _LOGGER.error(
        "[__init__.async_setup_entry] Invalid configuration: missing DB URL or connection details (db_user=%s, db_host=%s) — set '%s' in the config entry or YAML.",
        db_user,
        db_host,
        CONF_DB_URL,
    )
    return None


def _build_entity_filter(get_config) -> Callable[[str], bool]:
    """Build the entity filter, with excludes winning over include globs.

    Home Assistant's `generate_filter` (case 4a) lets `include_entity_globs`
    short-circuit *over* `exclude_entity_globs`: when an entity matches an
    include glob, the exclude globs are never checked. Scribe users expect the
    opposite, mirroring how `exclude_entities` already takes precedence.
    See https://github.com/jonathan-gtd/scribe/issues/33.
    """
    exclude_entities = get_config(CONF_EXCLUDE_ENTITIES, [])
    exclude_entity_globs = get_config(CONF_EXCLUDE_ENTITY_GLOBS, [])

    upstream = generate_filter(
        get_config(CONF_INCLUDE_DOMAINS, []),
        get_config(CONF_INCLUDE_ENTITIES, []),
        get_config(CONF_EXCLUDE_DOMAINS, []),
        exclude_entities,
        get_config(CONF_INCLUDE_ENTITY_GLOBS, []),
        exclude_entity_globs,
    )
    return _build_exclude_priority_filter(
        upstream, exclude_entities, exclude_entity_globs
    )


def _resolve_settings(hass: HomeAssistant, entry: ConfigEntry) -> "_Settings | None":
    """Resolve every setting: YAML > entry options > entry data > default.

    Returns None when the database URL cannot be determined, which is the one
    condition that makes setup impossible rather than merely degraded.
    """
    config = entry.data
    options = entry.options
    # YAML stays available for power users to override what the UI stores.
    yaml_config = hass.data[DOMAIN].get("yaml_config", {})

    def get_config(key, default, from_entry_data=True):
        if key in yaml_config:
            return yaml_config[key]
        if key in options:
            return options[key]
        if from_entry_data and key in config:
            return config[key]
        return default

    db_url = _resolve_db_url(config, yaml_config)
    if db_url is None:
        return None

    enable_stats_chunk = get_config(CONF_ENABLE_STATS_CHUNK, DEFAULT_ENABLE_STATS_CHUNK)
    enable_stats_size = get_config(CONF_ENABLE_STATS_SIZE, DEFAULT_ENABLE_STATS_SIZE)

    return _Settings(
        writer=WriterConfig(
            db_url=db_url,
            chunk_interval=get_config(
                CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL
            ),
            compress_after=get_config(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER),
            # Retention deletes history, so a value must never outlive the line
            # that asked for it. A YAML import copies its keys into the entry's
            # *data*, which nothing removes when the key later disappears from
            # configuration.yaml — reading it back would keep dropping chunks
            # the user stopped asking to drop. The UI writes retention to the
            # entry's options (the initial config flow only ever stores `db_url`
            # in data), so ignoring the data here makes "no setting" mean "keep
            # forever" on both paths.
            retention_states=get_config(
                CONF_RETENTION_STATES, DEFAULT_RETENTION_STATES, from_entry_data=False
            ),
            retention_events=get_config(
                CONF_RETENTION_EVENTS, DEFAULT_RETENTION_EVENTS, from_entry_data=False
            ),
            record_states=get_config(CONF_RECORD_STATES, DEFAULT_RECORD_STATES),
            record_events=get_config(CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS),
            batch_size=int(get_config(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE)),
            flush_interval=int(get_config(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL)),
            max_queue_size=int(get_config(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE)),
            buffer_on_failure=get_config(
                CONF_BUFFER_ON_FAILURE, DEFAULT_BUFFER_ON_FAILURE
            ),
            use_ssl=get_config(CONF_DB_SSL, DEFAULT_DB_SSL),
            ssl_root_cert=get_config(CONF_SSL_ROOT_CERT, None),
            ssl_cert_file=get_config(CONF_SSL_CERT_FILE, None),
            ssl_key_file=get_config(CONF_SSL_KEY_FILE, None),
            enable_table_areas=get_config(CONF_ENABLE_AREAS, DEFAULT_ENABLE_AREAS),
            enable_table_devices=get_config(
                CONF_ENABLE_DEVICES, DEFAULT_ENABLE_DEVICES
            ),
            enable_table_integrations=get_config(
                CONF_ENABLE_INTEGRATIONS, DEFAULT_ENABLE_INTEGRATIONS
            ),
            enable_table_users=get_config(CONF_ENABLE_USERS, DEFAULT_ENABLE_USERS),
            enable_stats_io=get_config(CONF_ENABLE_STATS_IO, DEFAULT_ENABLE_STATS_IO),
        ),
        stats_chunk_minutes=int(
            get_config(CONF_STATS_CHUNK_INTERVAL, DEFAULT_STATS_CHUNK_INTERVAL)
        )
        if enable_stats_chunk
        else None,
        stats_size_minutes=int(
            get_config(CONF_STATS_SIZE_INTERVAL, DEFAULT_STATS_SIZE_INTERVAL)
        )
        if enable_stats_size
        else None,
        entity_filter=_build_entity_filter(get_config),
        exclude_attributes=set(get_config(CONF_EXCLUDE_ATTRIBUTES, [])),
        include_events=set(get_config(CONF_INCLUDE_EVENTS, [])),
        exclude_events=set(get_config(CONF_EXCLUDE_EVENTS, [])),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scribe from a config entry.

    This is the main setup function called when the integration is loaded.
    It initializes the writer, connects to the database, and sets up event listeners.
    """
    _LOGGER.debug(
        "[__init__.async_setup_entry] Setting up Scribe entry: %s", entry.entry_id
    )
    hass.data.setdefault(DOMAIN, {})

    cfg = _resolve_settings(hass, entry)
    if cfg is None:
        return False

    # Read back by sensor.py when it decides which entities to create.
    hass.data[DOMAIN][entry.entry_id] = {"enable_stats_io": cfg.enable_stats_io}

    try:
        # Initialize Writer
        writer = ScribeWriter(hass, cfg.writer)

        # Start the writer task (async)
        await writer.start()

        # Sync Metadata and Refresh Coordinators in background to prevent bootstrap timeout
        async def _async_late_setup():
            """Push the registries into the database once setup has returned."""
            _LOGGER.debug(
                "[__init__._async_late_setup] Starting background Scribe setup tasks..."
            )
            await _sync_metadata(hass, writer)
            await _refresh_coordinators(chunk_coordinator, size_coordinator)
            _LOGGER.debug(
                "[__init__._async_late_setup] Background Scribe setup tasks completed"
            )

        chunk_coordinator, size_coordinator = _create_coordinators(
            hass,
            writer,
            chunk_minutes=cfg.stats_chunk_minutes,
            size_minutes=cfg.stats_size_minutes,
        )

        # Finalize hass.data
        hass.data[DOMAIN][entry.entry_id].update(
            {
                "writer": writer,
                "chunk_coordinator": chunk_coordinator,
                "size_coordinator": size_coordinator,
            }
        )

        # Launch background metadata sync and coordinator refreshes
        hass.async_create_task(_async_late_setup())

        # Forward setup to platforms (Sensor, Binary Sensor)
        await hass.config_entries.async_forward_entry_setups(
            entry, ["sensor", "binary_sensor"]
        )

        # Event listeners. Both are built outside this function: they are the
        # hot path, and reading them here would mean reading setup as well.
        _LOGGER.debug(
            "[__init__.async_setup_entry] Registering event listener (record_states=%s, record_events=%s)",
            cfg.record_states,
            cfg.record_events,
        )

        if cfg.record_states:
            entry.async_on_unload(
                hass.bus.async_listen(
                    EVENT_STATE_CHANGED,
                    _make_state_listener(
                        writer, cfg.entity_filter, cfg.exclude_attributes
                    ),
                )
            )

        if cfg.record_events:
            _register_event_listeners(
                hass, entry, writer, cfg.include_events, cfg.exclude_events
            )

        # Real-time metadata sync: keep each registry's table current as
        # Home Assistant changes it, rather than only at startup.
        entry.async_on_unload(
            hass.bus.async_listen(
                "entity_registry_updated",
                _make_entity_registry_listener(hass, writer),
            )
        )

        if writer.enable_table_devices:
            entry.async_on_unload(
                hass.bus.async_listen(
                    "device_registry_updated",
                    _make_registry_listener(
                        hass,
                        label="handle_device_registry_update",
                        id_key="device_id",
                        lookup=lambda h, i: dr.async_get(h).async_get(i),
                        build_row=_device_row,
                        write=writer.write_devices,
                    ),
                )
            )

        if writer.enable_table_areas:
            entry.async_on_unload(
                hass.bus.async_listen(
                    "area_registry_updated",
                    _make_registry_listener(
                        hass,
                        label="handle_area_registry_update",
                        id_key="area_id",
                        lookup=lambda h, i: ar.async_get(h).async_get_area(i),
                        build_row=_area_row,
                        write=writer.write_areas,
                    ),
                )
            )

        if writer.enable_table_users:
            user_listener = _make_user_listener(hass, writer)
            for event_type in ("user_added", "user_updated", "user_removed"):
                entry.async_on_unload(hass.bus.async_listen(event_type, user_listener))

        # Stop on FINAL_WRITE, not STOP. Home Assistant shuts down in stages —
        # `homeassistant_stop`, then `homeassistant_final_write`, then close —
        # and within a single event its specific listeners run before the
        # MATCH_ALL ones. Stopping on `homeassistant_stop` therefore ran the
        # final flush *before* Scribe's own event listener had been handed that
        # very event, so everything fired during shutdown was dropped: the stop
        # event itself, and every state change the integrations emit as they
        # unload. FINAL_WRITE is the stage meant for exactly this, and it is
        # what Home Assistant's own recorder uses to commit its last data.
        async def async_stop_scribe(event):
            await writer.stop()

        entry.async_on_unload(
            hass.bus.async_listen(EVENT_HOMEASSISTANT_FINAL_WRITE, async_stop_scribe)
        )

        _register_services(hass, writer)

        # Reload entry when options change (e.g. via Options Flow)
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

        return True

    except Exception as e:
        _LOGGER.error(
            "[__init__.async_setup_entry] Failed to setup Scribe integration: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return False


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry.

    Called when options are updated. Unloads and re-loads the integration to apply changes.
    """
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Called when the integration is removed or reloaded.
    Stops the writer thread and unloads platforms.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, ["sensor", "binary_sensor"]
    )
    if unload_ok:
        _LOGGER.debug(
            "[__init__.async_unload_entry] Unloading Scribe entry %s", entry.entry_id
        )
        data = hass.data[DOMAIN].pop(entry.entry_id)
        writer = data["writer"]
        # Ensure writer flushes remaining data before stopping
        await writer.stop()
        _LOGGER.debug(
            "[__init__.async_unload_entry] Scribe entry %s unloaded successfully",
            entry.entry_id,
        )
    return unload_ok
