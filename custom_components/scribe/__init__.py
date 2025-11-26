"""Scribe: A custom component to store Home Assistant history in TimescaleDB.

This component intercepts all state changes and events in Home Assistant and asynchronously
writes them to a TimescaleDB (PostgreSQL) database. It uses a dedicated writer task
to ensure that database operations do not block the main Home Assistant event loop.
"""
import logging
import json
import voluptuous as vol
from homeassistant.helpers.json import JSONEncoder

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entityfilter import generate_filter

from .const import (
    DOMAIN,
    CONF_DB_URL,
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
    CONF_TABLE_NAME_STATES,
    CONF_TABLE_NAME_EVENTS,
    CONF_ENABLE_STATISTICS,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_TABLE_NAME_STATES,
    DEFAULT_TABLE_NAME_EVENTS,
    DEFAULT_ENABLE_STATISTICS,
    CONF_BUFFER_ON_FAILURE,
    DEFAULT_BUFFER_ON_FAILURE,
)
from .writer import ScribeWriter

_LOGGER = logging.getLogger(__name__)

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import discovery

# Configuration Schema for YAML configuration
# This allows users to configure Scribe via configuration.yaml instead of the UI.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DB_URL): cv.string,
                vol.Optional(CONF_CHUNK_TIME_INTERVAL, default=DEFAULT_CHUNK_TIME_INTERVAL): cv.string,
                vol.Optional(CONF_COMPRESS_AFTER, default=DEFAULT_COMPRESS_AFTER): cv.string,
                vol.Optional(CONF_RECORD_STATES, default=DEFAULT_RECORD_STATES): cv.boolean,
                vol.Optional(CONF_RECORD_EVENTS, default=DEFAULT_RECORD_EVENTS): cv.boolean,
                vol.Optional(CONF_BATCH_SIZE, default=DEFAULT_BATCH_SIZE): cv.positive_int,
                vol.Optional(CONF_FLUSH_INTERVAL, default=DEFAULT_FLUSH_INTERVAL): cv.positive_int,
                vol.Optional(CONF_MAX_QUEUE_SIZE, default=DEFAULT_MAX_QUEUE_SIZE): cv.positive_int,
                vol.Optional(CONF_TABLE_NAME_STATES, default=DEFAULT_TABLE_NAME_STATES): cv.string,
                vol.Optional(CONF_TABLE_NAME_EVENTS, default=DEFAULT_TABLE_NAME_EVENTS): cv.string,
                vol.Optional(CONF_BUFFER_ON_FAILURE, default=DEFAULT_BUFFER_ON_FAILURE): cv.boolean,
                vol.Optional(CONF_ENABLE_STATISTICS, default=DEFAULT_ENABLE_STATISTICS): cv.boolean,
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
        
        # Check if an entry already exists
        current_entries = hass.config_entries.async_entries(DOMAIN)
        if current_entries:
            # Update the existing entry directly to avoid UnknownFlow race condition
            entry = current_entries[0]
            _LOGGER.debug(f"Updating existing Scribe entry {entry.entry_id} from YAML")
            hass.config_entries.async_update_entry(entry, data=config[DOMAIN])
            # Reload to apply changes if needed (optional, but good practice if options changed)
            # await hass.config_entries.async_reload(entry.entry_id) 
            # Note: async_update_entry might trigger reload listeners if configured, 
            # but usually we rely on the entry setup to pick up changes or a reload.
            # Since we are in async_setup, the entry might not be loaded yet or is about to be loaded.
            return True

        _LOGGER.debug("Found scribe configuration in YAML, triggering import flow")
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
    table_name_states = yaml_config.get(CONF_TABLE_NAME_STATES, DEFAULT_TABLE_NAME_STATES)
    table_name_events = yaml_config.get(CONF_TABLE_NAME_EVENTS, DEFAULT_TABLE_NAME_EVENTS)
    max_queue_size = yaml_config.get(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE)

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
        table_name_states=table_name_states,
        table_name_events=table_name_events
    )
    
    # Start the writer task (async)
    await writer.start()
    
    # Setup Data Update Coordinator for statistics
    from .coordinator import ScribeDataUpdateCoordinator
    
    coordinator = None
    enable_statistics = options.get(CONF_ENABLE_STATISTICS, config.get(CONF_ENABLE_STATISTICS, DEFAULT_ENABLE_STATISTICS))
    if enable_statistics:
        coordinator = ScribeDataUpdateCoordinator(hass, writer)
        await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "writer": writer,
        "coordinator": coordinator
    }

    # Forward setup to platforms (Sensor, Binary Sensor)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "binary_sensor"])

    # Event Listener
    async def handle_event(event: Event):
        """Handle incoming Home Assistant events.
        
        This function is called for EVERY event in Home Assistant.
        It filters the event and enqueues it for writing if it matches criteria.
        """
        event_type = event.event_type
        
        # Handle State Changes
        if event_type == EVENT_STATE_CHANGED:
            if not record_states:
                return
                
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")

            if new_state is None:
                return

            # Apply Include/Exclude Filter
            if not entity_filter(entity_id):
                return

            try:
                state_val = float(new_state.state)
            except (ValueError, TypeError):
                state_val = None

            # Filter attributes
            attributes = dict(new_state.attributes)
            if exclude_attributes:
                for attr in list(attributes.keys()):
                    if attr in exclude_attributes:
                        del attributes[attr]

            try:
                data = {
                    "type": "state",
                    "time": new_state.last_updated,
                    "entity_id": entity_id,
                    "state": new_state.state,
                    "value": state_val,
                    "attributes": json.dumps(attributes, cls=JSONEncoder),
                }
                _LOGGER.debug(f"Scribe: Enqueueing state change for {entity_id}")
                writer.enqueue(data)
            except Exception as e:
                _LOGGER.error(f"Scribe: Error processing state change for {entity_id}: {e}", exc_info=True)
            return

        # Handle Generic Events
        if record_events:
            try:
                data = {
                    "type": "event",
                    "time": event.time_fired,
                    "event_type": event_type,
                    "event_data": json.dumps(event.data, cls=JSONEncoder),
                    "origin": str(event.origin),
                    "context_id": event.context.id,
                    "context_user_id": event.context.user_id,
                    "context_parent_id": event.context.parent_id,
                }
                # _LOGGER.debug(f"Scribe: Enqueueing event {event_type}")
                writer.enqueue(data)
            except Exception as e:
                _LOGGER.error(f"Scribe: Error processing event {event_type}: {e}", exc_info=True)

    # Register the event listener
    # Listening to None means we listen to ALL events
    entry.async_on_unload(
        hass.bus.async_listen(None, handle_event) 
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
        _LOGGER.debug("Manual flush triggered via service call")
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
