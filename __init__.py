"""Scribe: A custom component to store Home Assistant history in TimescaleDB."""
import logging
import json
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_TIME_CHANGED,
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
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
)
from .writer import ScribeWriter

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Scribe component from YAML (not supported)."""
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

    writer = ScribeWriter(hass, db_url, chunk_interval, compress_after, record_states, record_events)
    
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
        
        # Skip time changes (too noisy)
        if event_type == EVENT_TIME_CHANGED:
            return

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
