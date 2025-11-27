"""Scribe: A custom component to store Home Assistant history in TimescaleDB.

This component intercepts all state changes and events in Home Assistant and asynchronously
writes them to a TimescaleDB (PostgreSQL) database. It uses a dedicated writer task
to ensure that database operations do not block the main Home Assistant event loop.
"""
import logging
import json

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.entityfilter import generate_filter
from homeassistant.helpers.json import JSONEncoder
from homeassistant.loader import bind_hass

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
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_ATTRIBUTES,
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
                vol.Optional(CONF_DB_SSL, default=DEFAULT_DB_SSL): cv.boolean,
                vol.Optional(CONF_SSL_ROOT_CERT): cv.string,
                vol.Optional(CONF_SSL_CERT_FILE): cv.string,
                vol.Optional(CONF_SSL_KEY_FILE): cv.string,
                vol.Optional(CONF_CHUNK_TIME_INTERVAL, default=DEFAULT_CHUNK_TIME_INTERVAL): cv.string,
                vol.Optional(CONF_COMPRESS_AFTER, default=DEFAULT_COMPRESS_AFTER): cv.string,
                vol.Optional(CONF_RECORD_STATES, default=DEFAULT_RECORD_STATES): cv.boolean,
                vol.Optional(CONF_RECORD_EVENTS, default=DEFAULT_RECORD_EVENTS): cv.boolean,
                vol.Optional(CONF_BATCH_SIZE, default=DEFAULT_BATCH_SIZE): cv.positive_int,
                vol.Optional(CONF_FLUSH_INTERVAL, default=DEFAULT_FLUSH_INTERVAL): cv.positive_int,
                vol.Optional(CONF_MAX_QUEUE_SIZE, default=DEFAULT_MAX_QUEUE_SIZE): cv.positive_int,
                vol.Optional(CONF_BUFFER_ON_FAILURE, default=DEFAULT_BUFFER_ON_FAILURE): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_IO, default=DEFAULT_ENABLE_STATS_IO): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_CHUNK, default=DEFAULT_ENABLE_STATS_CHUNK): cv.boolean,
                vol.Optional(CONF_ENABLE_STATS_SIZE, default=DEFAULT_ENABLE_STATS_SIZE): cv.boolean,
                vol.Optional(CONF_STATS_CHUNK_INTERVAL, default=DEFAULT_STATS_CHUNK_INTERVAL): cv.positive_int,
                vol.Optional(CONF_STATS_SIZE_INTERVAL, default=DEFAULT_STATS_SIZE_INTERVAL): cv.positive_int,
                vol.Optional(CONF_INCLUDE_DOMAINS, default=[]): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_INCLUDE_ENTITIES, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
                vol.Optional(CONF_EXCLUDE_DOMAINS, default=[]): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_EXCLUDE_ENTITIES, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
                vol.Optional(CONF_EXCLUDE_ATTRIBUTES, default=[]): vol.All(cv.ensure_list, [cv.string]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Scribe component from YAML.
    
    This function is called when Home Assistant starts and finds a 'scribe:' entry in configuration.yaml.
    It triggers the import flow to create a config entry if one doesn't exist.
    """
    hass.data.setdefault(DOMAIN, {})
    
    if DOMAIN in config:
        _LOGGER.info("Scribe configuration found in YAML. Verifying setup...")
        hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]
        
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=config[DOMAIN]
            )
        )
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scribe from a config entry.
    
    This is the main setup function called when the integration is loaded.
    It initializes the writer, connects to the database, and sets up event listeners.
    """
    _LOGGER.debug(f"Setting up Scribe entry: {entry.entry_id}")
    config = entry.data
    hass.data.setdefault(DOMAIN, {})

    options = entry.options

    # Advanced Config (YAML Only)
    # Some settings are only available via YAML to keep the UI simple.
    yaml_config = hass.data[DOMAIN].get("yaml_config", {})
    chunk_interval = yaml_config.get(CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL)
    compress_after = yaml_config.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER)
    
    # Get DB Config
    # Supports both legacy single URL and new individual fields (legacy support removed for simplicity in internal logic)
    if CONF_DB_URL in config:
        db_url = config[CONF_DB_URL]
    else:
        # Fallback for old configs that might still have individual fields
        # We construct the URL here
        db_user = config.get("db_user")
        db_pass = config.get("db_password")
        db_host = config.get("db_host")
        db_port = config.get("db_port")
        db_name = config.get("db_name")
        if db_user and db_host:
             db_url = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        else:
            _LOGGER.error("Invalid configuration: Missing DB URL or connection details")
            return False

    # YAML Only Settings
    # max_queue_size = yaml_config.get(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE)

    # Entity Filter
    # Sets up the include/exclude logic for domains and entities
    include_domains = options.get(CONF_INCLUDE_DOMAINS, [])
    include_entities = options.get(CONF_INCLUDE_ENTITIES, [])
    exclude_domains = options.get(CONF_EXCLUDE_DOMAINS, [])
    exclude_entities = options.get(CONF_EXCLUDE_ENTITIES, [])
    exclude_attributes = set(options.get(CONF_EXCLUDE_ATTRIBUTES, []))
    
    # Merge with YAML exclude attributes if present
    if CONF_EXCLUDE_ATTRIBUTES in yaml_config:
        exclude_attributes.update(yaml_config[CONF_EXCLUDE_ATTRIBUTES])

    entity_filter = generate_filter(
        include_domains,
        include_entities,
        exclude_domains,
        exclude_entities,
    )

    # Determine record_states and record_events for handle_event
    # Prioritizes Options Flow > Config Entry > Default
    record_states = options.get(CONF_RECORD_STATES, config.get(CONF_RECORD_STATES, DEFAULT_RECORD_STATES))
    record_events = options.get(CONF_RECORD_EVENTS, config.get(CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS))
    
    # SSL configuration (from YAML only)
    db_ssl = yaml_config.get(CONF_DB_SSL, DEFAULT_DB_SSL)
    
    # SSL Paths (from Config Entry or YAML)
    ssl_root_cert = config.get(CONF_SSL_ROOT_CERT) or yaml_config.get(CONF_SSL_ROOT_CERT)
    ssl_cert_file = config.get(CONF_SSL_CERT_FILE) or yaml_config.get(CONF_SSL_CERT_FILE)
    ssl_key_file = config.get(CONF_SSL_KEY_FILE) or yaml_config.get(CONF_SSL_KEY_FILE)

    # Initialize Writer
    # The ScribeWriter runs in a separate thread to handle DB I/O
    writer = ScribeWriter(
        hass=hass,
        db_url=db_url,
        chunk_interval=chunk_interval,
        compress_after=compress_after,
        record_states=record_states,
        record_events=record_events,
        batch_size=options.get(CONF_BATCH_SIZE, config.get(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE)),
        flush_interval=options.get(CONF_FLUSH_INTERVAL, config.get(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL)),
        max_queue_size=options.get(CONF_MAX_QUEUE_SIZE, config.get(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE)),
        buffer_on_failure=options.get(CONF_BUFFER_ON_FAILURE, config.get(CONF_BUFFER_ON_FAILURE, DEFAULT_BUFFER_ON_FAILURE)),
        table_name_states=DEFAULT_TABLE_NAME_STATES,
        table_name_events=DEFAULT_TABLE_NAME_EVENTS,
        use_ssl=db_ssl,
        ssl_root_cert=ssl_root_cert,
        ssl_cert_file=ssl_cert_file,
        ssl_key_file=ssl_key_file,
    )
    
    # Start the writer task (async)
    await writer.start()

    # Sync Users
    try:
        users_list = await hass.auth.async_get_users()
        _LOGGER.debug(f"Syncing users. Total users in hass.auth: {len(users_list)}")
        users = []
        for user in users_list:
            users.append({
                "user_id": user.id,
                "name": user.name,
                "is_owner": user.is_owner,
                "is_active": user.is_active,
                "system_generated": user.system_generated,
                "group_ids": json.dumps([g.id for g in user.groups], default=str)
            })
        
        if users:
            _LOGGER.debug(f"Calling writer.write_users with {len(users)} users")
            await writer.write_users(users)
        else:
            _LOGGER.warning("No users found to sync!")
            
    except Exception as e:
        _LOGGER.error(f"Error syncing users: {e}", exc_info=True)

    # Sync Entities
    try:
        registry = er.async_get(hass)
        entities = []
        for entity in registry.entities.values():
            entities.append({
                "entity_id": entity.entity_id,
                "unique_id": entity.unique_id,
                "platform": entity.platform,
                "domain": entity.domain,
                "name": entity.name or entity.original_name,
                "device_id": entity.device_id,
                "area_id": entity.area_id,
                "capabilities": json.dumps(entity.capabilities, default=str) if entity.capabilities else None
            })
        
        if entities:
            _LOGGER.debug(f"Syncing {len(entities)} entities to database")
            await writer.write_entities(entities)
    except Exception as e:
        _LOGGER.error(f"Error syncing entities: {e}", exc_info=True)

    # Sync Areas
    try:
        area_reg = ar.async_get(hass)
        areas = []
        for area in area_reg.areas.values():
            areas.append({
                "area_id": area.id,
                "name": area.name,
                "picture": area.picture
            })
        if areas:
            _LOGGER.debug(f"Syncing {len(areas)} areas to database")
            await writer.write_areas(areas)
    except Exception as e:
        _LOGGER.error(f"Error syncing areas: {e}", exc_info=True)

    # Sync Devices
    try:
        device_reg = dr.async_get(hass)
        devices = []
        for device in device_reg.devices.values():
            # Get primary config entry
            primary_entry = next(iter(device.config_entries), None) if device.config_entries else None
            
            devices.append({
                "device_id": device.id,
                "name": device.name,
                "name_by_user": device.name_by_user,
                "model": device.model,
                "manufacturer": device.manufacturer,
                "sw_version": device.sw_version,
                "area_id": device.area_id,
                "primary_config_entry": primary_entry
            })
        if devices:
            _LOGGER.debug(f"Syncing {len(devices)} devices to database")
            await writer.write_devices(devices)
    except Exception as e:
        _LOGGER.error(f"Error syncing devices: {e}", exc_info=True)

    # Sync Integrations (Config Entries)
    try:
        integrations = []
        for config_entry in hass.config_entries.async_entries():
            integrations.append({
                "entry_id": config_entry.entry_id,
                "domain": config_entry.domain,
                "title": config_entry.title,
                "state": config_entry.state.value if hasattr(config_entry.state, "value") else str(config_entry.state),
                "source": config_entry.source
            })
        if integrations:
            _LOGGER.debug(f"Syncing {len(integrations)} integrations to database")
            await writer.write_integrations(integrations)
    except Exception as e:
        _LOGGER.error(f"Error syncing integrations: {e}", exc_info=True)
    
    # Setup Data Update Coordinators for statistics
    from .coordinator import ScribeDataUpdateCoordinator
    
    chunk_coordinator = None
    size_coordinator = None
    
    # Check granular settings
    enable_stats_chunk = options.get(CONF_ENABLE_STATS_CHUNK, config.get(CONF_ENABLE_STATS_CHUNK, DEFAULT_ENABLE_STATS_CHUNK))
    enable_stats_size = options.get(CONF_ENABLE_STATS_SIZE, config.get(CONF_ENABLE_STATS_SIZE, DEFAULT_ENABLE_STATS_SIZE))
    
    if enable_stats_chunk:
        chunk_interval = options.get(CONF_STATS_CHUNK_INTERVAL, config.get(CONF_STATS_CHUNK_INTERVAL, DEFAULT_STATS_CHUNK_INTERVAL))
        chunk_coordinator = ScribeDataUpdateCoordinator(
            hass, 
            writer, 
            update_interval_minutes=chunk_interval,
            stats_type="chunk"
        )
        await chunk_coordinator.async_config_entry_first_refresh()
    
    if enable_stats_size:
        size_interval = options.get(CONF_STATS_SIZE_INTERVAL, config.get(CONF_STATS_SIZE_INTERVAL, DEFAULT_STATS_SIZE_INTERVAL))
        size_coordinator = ScribeDataUpdateCoordinator(
            hass, 
            writer, 
            update_interval_minutes=size_interval,
            stats_type="size"
        )
        await size_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "writer": writer,
        "chunk_coordinator": chunk_coordinator,
        "size_coordinator": size_coordinator,
    }

    # Forward setup to platforms (Sensor, Binary Sensor)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "binary_sensor"])

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
            _LOGGER.debug(f"Entity {entity_id} filtered out")
            return

        try:
            state_val = float(new_state.state)
            state_str = None
        except (ValueError, TypeError):
            state_val = None
            state_str = new_state.state

        # Filter Attributes
        filtered_attrs = {k: v for k, v in new_state.attributes.items() if k not in exclude_attributes}

        try:
            writer.enqueue({
                "type": "state",
                "time": new_state.last_updated,
                "entity_id": entity_id,
                "state": state_str,
                "value": state_val,
                "attributes": json.dumps(filtered_attrs, default=str),
            })
        except Exception as e:
            _LOGGER.error(f"Error enqueueing state for {entity_id}: {e}")

    # Register the event listener for state changes
    _LOGGER.debug(f"Registering event listener (record_states={record_states}, record_events={record_events})")
    
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

            _other_event_count["total"] += 1
            if _other_event_count["total"] <= 5:
                _LOGGER.debug(f"handle_other_events called: {event.event_type}")
            
            try:
                data = {
                    "type": "event",
                    "time": event.time_fired,
                    "event_type": event.event_type,
                    "event_data": json.dumps(event.data, cls=JSONEncoder),
                    "origin": str(event.origin),
                    "context_id": event.context.id,
                    "context_user_id": event.context.user_id,
                    "context_parent_id": event.context.parent_id,
                }
                writer.enqueue(data)
            except Exception as e:
                _LOGGER.error(f"Scribe: Error processing event {event.event_type}: {e}", exc_info=True)
        
        # Use MATCH_ALL to listen to all events
        from homeassistant.const import MATCH_ALL
        _LOGGER.debug("Registering listener for ALL events (MATCH_ALL)")																												
        entry.async_on_unload(
            hass.bus.async_listen(MATCH_ALL, handle_other_events)
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
        _LOGGER.info("Manual flush triggered via service call")
        await writer._flush()
        
    hass.services.async_register(DOMAIN, "flush", handle_flush)

    # Reload entry when options change (e.g. via Options Flow)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True

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
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "binary_sensor"])
    if unload_ok:
        _LOGGER.debug("Unloading Scribe entry")
        data = hass.data[DOMAIN].pop(entry.entry_id)
        writer = data["writer"]
        # Ensure writer flushes remaining data before stopping
        await writer.stop()
        _LOGGER.debug("Scribe entry unloaded successfully")
    return unload_ok