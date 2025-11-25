"""Scribe: A custom component to store Home Assistant history in TimescaleDB."""
import logging
import json
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
    EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.helpers.typing import ConfigType
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
    CONF_RECORD_STATES,
    CONF_RECORD_EVENTS,
    CONF_BATCH_SIZE,
    CONF_FLUSH_INTERVAL,
    CONF_TABLE_NAME_STATES,
    CONF_TABLE_NAME_EVENTS,
    CONF_DEBUG,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_TABLE_NAME_STATES,
    DEFAULT_TABLE_NAME_EVENTS,
    DEFAULT_DEBUG,
)
from .writer import ScribeWriter

_LOGGER = logging.getLogger(__name__)

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import discovery

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
                vol.Optional(CONF_TABLE_NAME_STATES, default=DEFAULT_TABLE_NAME_STATES): cv.string,
                vol.Optional(CONF_TABLE_NAME_EVENTS, default=DEFAULT_TABLE_NAME_EVENTS): cv.string,
                vol.Optional(CONF_DEBUG, default=DEFAULT_DEBUG): cv.boolean,
                vol.Optional(CONF_INCLUDE_DOMAINS, default=[]): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_INCLUDE_ENTITIES, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
                vol.Optional(CONF_EXCLUDE_DOMAINS, default=[]): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_EXCLUDE_ENTITIES, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Scribe component from YAML."""
    hass.data.setdefault(DOMAIN, {})
    
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=config[DOMAIN]
            )
        )
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scribe from a config entry."""
    config = entry.data
    options = entry.options

    db_url = config[CONF_DB_URL]
    chunk_interval = options.get(CONF_CHUNK_TIME_INTERVAL, config.get(CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL))
    compress_after = options.get(CONF_COMPRESS_AFTER, config.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER))
    record_states = options.get(CONF_RECORD_STATES, config.get(CONF_RECORD_STATES, DEFAULT_RECORD_STATES))
    record_events = options.get(CONF_RECORD_EVENTS, config.get(CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS))
    
    # Advanced Config
    batch_size = options.get(CONF_BATCH_SIZE, config.get(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE))
    flush_interval = options.get(CONF_FLUSH_INTERVAL, config.get(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL))
    table_name_states = yaml_config.get(CONF_TABLE_NAME_STATES, DEFAULT_TABLE_NAME_STATES)
    table_name_events = yaml_config.get(CONF_TABLE_NAME_EVENTS, DEFAULT_TABLE_NAME_EVENTS)
    debug_mode = yaml_config.get(CONF_DEBUG, DEFAULT_DEBUG)

    if debug_mode:
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.debug("Debug mode enabled")

    # Entity Filter
    include_domains = options.get(CONF_INCLUDE_DOMAINS, [])
    include_entities = options.get(CONF_INCLUDE_ENTITIES, [])
    exclude_domains = options.get(CONF_EXCLUDE_DOMAINS, [])
    exclude_entities = options.get(CONF_EXCLUDE_ENTITIES, [])
    
    entity_filter = generate_filter(
        include_domains,
        include_entities,
        exclude_domains,
        exclude_entities,
    )

    writer = ScribeWriter(hass, db_url, chunk_interval, compress_after, record_states, record_events, batch_size, flush_interval, table_name_states, table_name_events)
    
    # Initialize DB (async)
    await hass.async_add_executor_job(writer.init_db)
    
    # Start writer
    writer.start()
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = writer

    # Listener
    async def handle_event(event: Event):
        """Handle incoming events."""
        event_type = event.event_type
        
        # Handle States
        if event_type == EVENT_STATE_CHANGED:
            if not record_states:
                return
                
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")

            if new_state is None:
                return

            if not entity_filter(entity_id):
                return

            try:
                state_val = float(new_state.state)
            except (ValueError, TypeError):
                state_val = None

            data = {
                "type": "state",
                "time": new_state.last_updated,
                "entity_id": entity_id,
                "state": new_state.state,
                "value": state_val,
                "attributes": json.dumps(dict(new_state.attributes), default=str),
            }
            writer.enqueue(data)
            return

        # Handle Other Events
        if record_events:
            data = {
                "type": "event",
                "time": event.time_fired,
                "event_type": event_type,
                "event_data": json.dumps(event.data, default=str),
                "origin": str(event.origin),
                "context_id": event.context.id,
                "context_user_id": event.context.user_id,
                "context_parent_id": event.context.parent_id,
            }
            writer.enqueue(data)

    # Listen to all events
    entry.async_on_unload(
        hass.bus.async_listen(None, handle_event) # None means all events
    )
    
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, writer.shutdown)
    )
    
    # Reload entry when options change
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    writer = hass.data[DOMAIN].pop(entry.entry_id)
    await hass.async_add_executor_job(writer.shutdown, None)
    return True
