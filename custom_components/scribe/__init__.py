"""Scribe: A custom component to store Home Assistant history in TimescaleDB.

This component intercepts all state changes and events in Home Assistant and asynchronously
writes them to a TimescaleDB (PostgreSQL) database. It uses a dedicated writer task
to ensure that database operations do not block the main Home Assistant event loop.
"""

import logging

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
    EVENT_HOMEASSISTANT_STOP,
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
    DEFAULT_DB_SSL,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_TABLE_NAME_STATES,
    DEFAULT_TABLE_NAME_EVENTS,
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
from .writer import ScribeWriter

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scribe from a config entry.

    This is the main setup function called when the integration is loaded.
    It initializes the writer, connects to the database, and sets up event listeners.
    """
    _LOGGER.debug(
        "[__init__.async_setup_entry] Setting up Scribe entry: %s", entry.entry_id
    )
    config = entry.data
    hass.data.setdefault(DOMAIN, {})

    options = entry.options

    # Advanced Config (YAML Only / Priority Overrides)
    # Some settings are available via YAML to allow power users to override UI settings.
    yaml_config = hass.data[DOMAIN].get("yaml_config", {})

    # Helper to resolve config: YAML > Options > Config Entry > Default
    def get_config(key, default):
        # 1. Check YAML (Priority)
        if key in yaml_config:
            return yaml_config[key]
        # 2. Check Entry Options (UI-level changes)
        if key in options:
            return options[key]
        # 3. Check Entry Data (Initial setup)
        if key in config:
            return config[key]
        # 4. Fallback
        return default

    # Get DB URL (special case, usually in config or reconstructed)
    if CONF_DB_URL in config:
        db_url = config[CONF_DB_URL]
    elif CONF_DB_URL in yaml_config:
        db_url = yaml_config[CONF_DB_URL]
    else:
        # Fallback for old configs
        db_user = config.get("db_user") or yaml_config.get("db_user")
        db_pass = config.get("db_password") or yaml_config.get("db_password")
        db_host = config.get("db_host") or yaml_config.get("db_host")
        db_port = config.get("db_port") or yaml_config.get("db_port")
        db_name = config.get("db_name") or yaml_config.get("db_name")
        if db_user and db_host:
            db_url = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        else:
            _LOGGER.error(
                "[__init__.async_setup_entry] Invalid configuration: missing DB URL or connection details (db_user=%s, db_host=%s) — set '%s' in the config entry or YAML.",
                db_user,
                db_host,
                CONF_DB_URL,
            )
            return False

    # Resolve all parameters
    chunk_interval = get_config(CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL)
    compress_after = get_config(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER)
    record_states = get_config(CONF_RECORD_STATES, DEFAULT_RECORD_STATES)
    record_events = get_config(CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS)
    batch_size = int(get_config(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE))
    flush_interval = int(get_config(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL))
    max_queue_size = int(get_config(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE))
    buffer_on_failure = get_config(CONF_BUFFER_ON_FAILURE, DEFAULT_BUFFER_ON_FAILURE)

    # Statistics Flags (This was the issue being reported)
    enable_stats_io = get_config(CONF_ENABLE_STATS_IO, DEFAULT_ENABLE_STATS_IO)
    enable_stats_chunk = get_config(CONF_ENABLE_STATS_CHUNK, DEFAULT_ENABLE_STATS_CHUNK)
    enable_stats_size = get_config(CONF_ENABLE_STATS_SIZE, DEFAULT_ENABLE_STATS_SIZE)

    # Store flags in hass.data for platform setup (e.g. sensor.py)
    hass.data[DOMAIN][entry.entry_id] = {
        "enable_stats_io": enable_stats_io,
    }

    # Filtering Logic
    include_domains = get_config(CONF_INCLUDE_DOMAINS, [])
    include_entities = get_config(CONF_INCLUDE_ENTITIES, [])
    include_entity_globs = get_config(CONF_INCLUDE_ENTITY_GLOBS, [])
    exclude_domains = get_config(CONF_EXCLUDE_DOMAINS, [])
    exclude_entities = get_config(CONF_EXCLUDE_ENTITIES, [])
    exclude_entity_globs = get_config(CONF_EXCLUDE_ENTITY_GLOBS, [])

    exclude_attributes = set(get_config(CONF_EXCLUDE_ATTRIBUTES, []))
    include_events = set(get_config(CONF_INCLUDE_EVENTS, []))
    exclude_events = set(get_config(CONF_EXCLUDE_EVENTS, []))

    entity_filter = generate_filter(
        include_domains,
        include_entities,
        exclude_domains,
        exclude_entities,
        include_entity_globs,
        exclude_entity_globs,
    )

    # Home Assistant's `generate_filter` (case 4a) lets `include_entity_globs`
    # short-circuit *over* `exclude_entity_globs`: when an entity matches an
    # include glob, the exclude globs are never checked. Scribe users expect
    # the opposite — `exclude_entity_globs` should override
    # `include_entity_globs`, mirroring how `exclude_entities` already takes
    # precedence over `include_entity_globs`.
    #
    # Wrap the filter so an exclude-glob match (or an exclude-entity match)
    # is always a hard reject, then defer to the upstream filter for
    # everything else. See https://github.com/jonathan-gtd/scribe/issues/33.
    entity_filter = _build_exclude_priority_filter(
        entity_filter,
        exclude_entities,
        exclude_entity_globs,
    )

    # SSL configuration
    db_ssl = get_config(CONF_DB_SSL, DEFAULT_DB_SSL)
    ssl_root_cert = get_config(CONF_SSL_ROOT_CERT, None)
    ssl_cert_file = get_config(CONF_SSL_CERT_FILE, None)
    ssl_key_file = get_config(CONF_SSL_KEY_FILE, None)

    try:
        # Initialize Writer
        writer = ScribeWriter(
            hass=hass,
            db_url=db_url,
            chunk_interval=chunk_interval,
            compress_after=compress_after,
            record_states=record_states,
            record_events=record_events,
            batch_size=batch_size,
            flush_interval=flush_interval,
            max_queue_size=max_queue_size,
            buffer_on_failure=buffer_on_failure,
            table_name_states=DEFAULT_TABLE_NAME_STATES,
            table_name_events=DEFAULT_TABLE_NAME_EVENTS,
            use_ssl=db_ssl,
            ssl_root_cert=ssl_root_cert,
            ssl_cert_file=ssl_cert_file,
            ssl_key_file=ssl_key_file,
            enable_table_areas=get_config(CONF_ENABLE_AREAS, DEFAULT_ENABLE_AREAS),
            enable_table_devices=get_config(
                CONF_ENABLE_DEVICES, DEFAULT_ENABLE_DEVICES
            ),
            enable_table_integrations=get_config(
                CONF_ENABLE_INTEGRATIONS, DEFAULT_ENABLE_INTEGRATIONS
            ),
            enable_table_users=get_config(CONF_ENABLE_USERS, DEFAULT_ENABLE_USERS),
        )

        # Start the writer task (async)
        await writer.start()

        # Sync Metadata and Refresh Coordinators in background to prevent bootstrap timeout
        async def _async_late_setup():
            """Perform metadata sync and coordinator refresh in background."""
            _LOGGER.debug(
                "[__init__._async_late_setup] Starting background Scribe setup tasks..."
            )

            # Sync Users
            if writer.enable_table_users:
                try:
                    users_list = await hass.auth.async_get_users()
                    _LOGGER.debug(
                        "[__init__._async_late_setup:users] Total users in hass.auth: %d",
                        len(users_list),
                    )
                    users = []
                    for user in users_list:
                        users.append(
                            {
                                "user_id": user.id,
                                "name": user.name,
                                "is_owner": user.is_owner,
                                "is_active": user.is_active,
                                "system_generated": user.system_generated,
                                "group_ids": [g.id for g in user.groups]
                                if user.groups
                                else [],
                            }
                        )

                    if users:
                        _LOGGER.debug(
                            "[__init__._async_late_setup:users] Calling writer.write_users with %d users",
                            len(users),
                        )
                        await writer.write_users(users)
                    else:
                        _LOGGER.warning(
                            "[__init__._async_late_setup:users] No users found to sync (hass.auth returned 0 users)"
                        )

                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup:users] Error syncing users: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            # Sync Entities
            try:
                registry = er.async_get(hass)
                entities = []
                for entity in registry.entities.values():
                    entities.append(
                        {
                            "entity_id": entity.entity_id,
                            "unique_id": entity.unique_id,
                            "platform": entity.platform,
                            "domain": entity.domain,
                            "name": entity.name or entity.original_name,
                            "device_id": entity.device_id,
                            "area_id": entity.area_id,
                            "capabilities": entity.capabilities
                            if entity.capabilities
                            else None,
                        }
                    )

                if entities:
                    _LOGGER.debug(
                        "[__init__._async_late_setup:entities] Syncing %d entities to database",
                        len(entities),
                    )
                    await writer.write_entities(entities)
            except Exception as e:
                _LOGGER.error(
                    "[__init__._async_late_setup:entities] Error syncing entities: %s (%s)",
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

            # Sync Areas
            if writer.enable_table_areas:
                try:
                    area_reg = ar.async_get(hass)
                    areas = []
                    for area in area_reg.areas.values():
                        areas.append(
                            {
                                "area_id": area.id,
                                "name": area.name,
                                "picture": area.picture,
                            }
                        )
                    if areas:
                        _LOGGER.debug(
                            "[__init__._async_late_setup:areas] Syncing %d areas to database",
                            len(areas),
                        )
                        await writer.write_areas(areas)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup:areas] Error syncing areas: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            # Sync Devices
            if writer.enable_table_devices:
                try:
                    device_reg = dr.async_get(hass)
                    devices = []
                    for device in device_reg.devices.values():
                        # Get primary config entry
                        primary_entry = (
                            next(iter(device.config_entries), None)
                            if device.config_entries
                            else None
                        )

                        devices.append(
                            {
                                "device_id": device.id,
                                "name": device.name,
                                "name_by_user": device.name_by_user,
                                "model": device.model,
                                "manufacturer": device.manufacturer,
                                "sw_version": device.sw_version,
                                "area_id": device.area_id,
                                "primary_config_entry": primary_entry,
                            }
                        )
                    if devices:
                        _LOGGER.debug(
                            "[__init__._async_late_setup:devices] Syncing %d devices to database",
                            len(devices),
                        )
                        await writer.write_devices(devices)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup:devices] Error syncing devices: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            # Sync Integrations (Config Entries)
            if writer.enable_table_integrations:
                try:
                    integrations = []
                    for config_entry in hass.config_entries.async_entries():
                        integrations.append(
                            {
                                "entry_id": config_entry.entry_id,
                                "domain": config_entry.domain,
                                "title": config_entry.title,
                                "state": config_entry.state.value
                                if hasattr(config_entry.state, "value")
                                else str(config_entry.state),
                                "source": config_entry.source,
                            }
                        )
                    if integrations:
                        _LOGGER.debug(
                            "[__init__._async_late_setup:integrations] Syncing %d integrations to database",
                            len(integrations),
                        )
                        await writer.write_integrations(integrations)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup:integrations] Error syncing integrations: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            # Refresh coordinators
            if chunk_coordinator:
                try:
                    await chunk_coordinator.async_refresh()
                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup] Failed to refresh chunk coordinator: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            if size_coordinator:
                try:
                    await size_coordinator.async_refresh()
                except Exception as e:
                    _LOGGER.error(
                        "[__init__._async_late_setup] Failed to refresh size coordinator: %s (%s)",
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            _LOGGER.debug(
                "[__init__._async_late_setup] Background Scribe setup tasks completed"
            )

        # Setup Data Update Coordinators for statistics (Synchronous creation)
        from .coordinator import ScribeDataUpdateCoordinator

        chunk_coordinator = None
        size_coordinator = None

        # Check granular settings (resolved above)
        if enable_stats_chunk:
            try:
                chunk_interval_min = int(
                    get_config(CONF_STATS_CHUNK_INTERVAL, DEFAULT_STATS_CHUNK_INTERVAL)
                )
                chunk_coordinator = ScribeDataUpdateCoordinator(
                    hass,
                    writer,
                    update_interval_minutes=chunk_interval_min,
                    stats_type="chunk",
                )
            except Exception as e:
                _LOGGER.error(
                    "[__init__.async_setup_entry] Failed to setup chunk coordinator (interval=%s): %s (%s)",
                    get_config(CONF_STATS_CHUNK_INTERVAL, DEFAULT_STATS_CHUNK_INTERVAL),
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

        if enable_stats_size:
            try:
                size_interval_min = int(
                    get_config(CONF_STATS_SIZE_INTERVAL, DEFAULT_STATS_SIZE_INTERVAL)
                )
                size_coordinator = ScribeDataUpdateCoordinator(
                    hass,
                    writer,
                    update_interval_minutes=size_interval_min,
                    stats_type="size",
                )
            except Exception as e:
                _LOGGER.error(
                    "[__init__.async_setup_entry] Failed to setup size coordinator (interval=%s): %s (%s)",
                    get_config(CONF_STATS_SIZE_INTERVAL, DEFAULT_STATS_SIZE_INTERVAL),
                    e,
                    type(e).__name__,
                    exc_info=True,
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

        # Event Listener - must be a sync callback, not async!
        @callback
        def handle_event(event: Event):
            """Handle state change events.

            Note: This must be a synchronous callback (not async) for proper event handling.
            """

            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")

            if new_state is None:
                return

            # Apply Include/Exclude Filter
            if not entity_filter(entity_id):
                _LOGGER.debug(
                    "[__init__.handle_event] Entity %s filtered out", entity_id
                )
                return

            try:
                state_val = float(new_state.state)
                state_str = None
            except (ValueError, TypeError):
                state_val = None
                state_str = new_state.state

            # Filter Attributes
            filtered_attrs = {
                k: v
                for k, v in new_state.attributes.items()
                if k not in exclude_attributes
            }

            try:
                writer.enqueue(
                    {
                        "type": "state",
                        "time": new_state.last_updated,
                        "entity_id": entity_id,
                        "state": state_str,
                        "value": state_val,
                        "attributes": filtered_attrs,
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

        # Register the event listener for state changes
        _LOGGER.debug(
            "[__init__.async_setup_entry] Registering event listener (record_states=%s, record_events=%s)",
            record_states,
            record_events,
        )

        if record_states:
            entry.async_on_unload(
                hass.bus.async_listen(EVENT_STATE_CHANGED, handle_event)
            )

        # For generic events, listen to all events (but handle_event will filter)
        if record_events:
            # Listen to all events except state_changed (already handled above)
            _other_event_count = {"total": 0}

            @callback
            def handle_other_events(event: Event):
                """Handle non-state-change events."""
                if event.event_type == EVENT_STATE_CHANGED:
                    return  # Already handled above

                if event.event_type in exclude_events:
                    return

                _other_event_count["total"] += 1
                if _other_event_count["total"] <= 5:
                    _LOGGER.debug(
                        "[__init__.handle_other_events] First events seen: %s",
                        event.event_type,
                    )

                try:
                    data = {
                        "type": "event",
                        "time": event.time_fired,
                        "event_type": event.event_type,
                        "event_data": event.data,
                        "origin": str(event.origin),
                        "context_id": event.context.id,
                        "context_user_id": event.context.user_id,
                        "context_parent_id": event.context.parent_id,
                    }
                    writer.enqueue(data)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__.handle_other_events] Error processing event %s: %s (%s)",
                        event.event_type,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            # Use MATCH_ALL to listen to all events
            if include_events:
                _LOGGER.debug(
                    "[__init__.async_setup_entry] Registering listeners for specific events: %s",
                    include_events,
                )
                for event_type in include_events:
                    entry.async_on_unload(
                        hass.bus.async_listen(event_type, handle_other_events)
                    )
            else:
                from homeassistant.const import MATCH_ALL

                _LOGGER.debug(
                    "[__init__.async_setup_entry] Registering listener for ALL events (MATCH_ALL)"
                )
                entry.async_on_unload(
                    hass.bus.async_listen(MATCH_ALL, handle_other_events)
                )

        # Real-time Metadata Sync Listeners

        # Entity Registry Updates
        async def handle_entity_registry_update(event: Event):
            """Handle entity registry update."""
            action = event.data.get("action")
            entity_id = event.data.get("entity_id")

            # Handle Rename
            if action == "update":
                old_entity_id = event.data.get("old_entity_id")
                if old_entity_id and old_entity_id != entity_id:
                    _LOGGER.debug(
                        "[__init__.handle_entity_registry_update] Entity renamed: %s -> %s",
                        old_entity_id,
                        entity_id,
                    )
                    await writer.rename_entity(old_entity_id, entity_id)

            if action in ["create", "update"]:
                _LOGGER.debug(
                    "[__init__.handle_entity_registry_update] Registry update: %s %s",
                    action,
                    entity_id,
                )
                try:
                    registry = er.async_get(hass)
                    entity = registry.async_get(entity_id)
                    if entity:
                        data = [
                            {
                                "entity_id": entity.entity_id,
                                "unique_id": entity.unique_id,
                                "platform": entity.platform,
                                "domain": entity.domain,
                                "name": entity.name or entity.original_name,
                                "device_id": entity.device_id,
                                "area_id": entity.area_id,
                                "capabilities": entity.capabilities
                                if entity.capabilities
                                else None,
                            }
                        ]
                        await writer.write_entities(data)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__.handle_entity_registry_update] Error syncing entity %s (action=%s): %s (%s)",
                        entity_id,
                        action,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

        entry.async_on_unload(
            hass.bus.async_listen(
                "entity_registry_updated", handle_entity_registry_update
            )
        )

        # Device Registry Updates
        if writer.enable_table_devices:

            async def handle_device_registry_update(event: Event):
                """Handle device registry update."""
                action = event.data.get("action")
                device_id = event.data.get("device_id")

                if action in ["create", "update"]:
                    _LOGGER.debug(
                        "[__init__.handle_device_registry_update] Registry update: %s %s",
                        action,
                        device_id,
                    )
                    try:
                        device_reg = dr.async_get(hass)
                        device = device_reg.async_get(device_id)
                        if device:
                            # Get primary config entry
                            primary_entry = (
                                next(iter(device.config_entries), None)
                                if device.config_entries
                                else None
                            )

                            data = [
                                {
                                    "device_id": device.id,
                                    "name": device.name,
                                    "name_by_user": device.name_by_user,
                                    "model": device.model,
                                    "manufacturer": device.manufacturer,
                                    "sw_version": device.sw_version,
                                    "area_id": device.area_id,
                                    "primary_config_entry": primary_entry,
                                }
                            ]
                            await writer.write_devices(data)
                    except Exception as e:
                        _LOGGER.error(
                            "[__init__.handle_device_registry_update] Error syncing device %s (action=%s): %s (%s)",
                            device_id,
                            action,
                            e,
                            type(e).__name__,
                            exc_info=True,
                        )

            entry.async_on_unload(
                hass.bus.async_listen(
                    "device_registry_updated", handle_device_registry_update
                )
            )

        # Area Registry Updates
        if writer.enable_table_areas:

            async def handle_area_registry_update(event: Event):
                """Handle area registry update."""
                action = event.data.get("action")
                area_id = event.data.get("area_id")

                if action in ["create", "update"]:
                    _LOGGER.debug(
                        "[__init__.handle_area_registry_update] Registry update: %s %s",
                        action,
                        area_id,
                    )
                    try:
                        area_reg = ar.async_get(hass)
                        area = area_reg.async_get_area(area_id)
                        if area:
                            data = [
                                {
                                    "area_id": area.id,
                                    "name": area.name,
                                    "picture": area.picture,
                                }
                            ]
                            await writer.write_areas(data)
                    except Exception as e:
                        _LOGGER.error(
                            "[__init__.handle_area_registry_update] Error syncing area %s (action=%s): %s (%s)",
                            area_id,
                            action,
                            e,
                            type(e).__name__,
                            exc_info=True,
                        )

            entry.async_on_unload(
                hass.bus.async_listen(
                    "area_registry_updated", handle_area_registry_update
                )
            )

        # User Registry Updates
        if writer.enable_table_users:

            async def handle_user_update(event: Event):
                """Handle user registry update."""
                user_id = event.data.get("user_id")
                action = event.event_type  # user_added, user_updated, user_removed

                _LOGGER.debug(
                    "[__init__.handle_user_update] Registry update: %s %s",
                    action,
                    user_id,
                )
                try:
                    user = await hass.auth.async_get_user(user_id)
                    if user:
                        data = [
                            {
                                "user_id": user.id,
                                "name": user.name,
                                "is_owner": user.is_owner,
                                "is_active": user.is_active,
                                "system_generated": user.system_generated,
                                "group_ids": [g.id for g in user.groups]
                                if user.groups
                                else [],
                            }
                        ]
                        await writer.write_users(data)
                except Exception as e:
                    _LOGGER.error(
                        "[__init__.handle_user_update] Error syncing user %s (action=%s): %s (%s)",
                        user_id,
                        action,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            entry.async_on_unload(
                hass.bus.async_listen("user_added", handle_user_update)
            )
            entry.async_on_unload(
                hass.bus.async_listen("user_updated", handle_user_update)
            )
            entry.async_on_unload(
                hass.bus.async_listen("user_removed", handle_user_update)
            )

        # Register shutdown handler
        async def async_stop_scribe(event):
            await writer.stop()

        entry.async_on_unload(
            hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, async_stop_scribe)
        )

        async def handle_flush(call):
            """Handle flush service call.

            Allows users to manually trigger a database flush via automation or UI.
            """
            _LOGGER.info(
                "[__init__.handle_flush] Manual flush triggered via service call"
            )
            await writer._flush()

        hass.services.async_register(DOMAIN, "flush", handle_flush)

        async def handle_query(call):
            """Handle query service call."""
            sql = call.data.get("sql")
            if not sql:
                raise HomeAssistantError("SQL query is required")

            try:
                result = await writer.query(sql)
                return {"result": result}
            except ValueError as e:
                # Specific validation error (e.g. non-SELECT query)
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

        hass.services.async_register(
            DOMAIN,
            "query",
            handle_query,
            schema=vol.Schema({vol.Required("sql"): cv.string}),
            supports_response=True,
        )

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
