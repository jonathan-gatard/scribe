"""Chronicle: A custom component to store Home Assistant history in TimescaleDB."""
import logging
import threading
import time
import json
import psycopg2
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_TABLE_NAME,
    CONF_CHUNK_TIME_INTERVAL,
    CONF_COMPRESS_AFTER,
    DEFAULT_TABLE_NAME,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
)

_LOGGER = logging.getLogger(__name__)

BATCH_SIZE = 100
FLUSH_INTERVAL = 5

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Chronicle component from YAML (not supported, but required signature)."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Chronicle from a config entry."""
    config = entry.data
    options = entry.options

    db_url = config[CONF_DB_URL]
    table_name = config.get(CONF_TABLE_NAME, DEFAULT_TABLE_NAME)
    chunk_interval = options.get(CONF_CHUNK_TIME_INTERVAL, config.get(CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL))
    compress_after = options.get(CONF_COMPRESS_AFTER, config.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER))

    handler = ChronicleHandler(hass, db_url, table_name, chunk_interval, compress_after)
    
    # Initialize DB (async)
    await hass.async_add_executor_job(handler.init_db)
    
    # Start handler
    handler.start()
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = handler

    # Listeners
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_STATE_CHANGED, handler.event_listener)
    )
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, handler.shutdown)
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    handler = hass.data[DOMAIN].pop(entry.entry_id)
    await hass.async_add_executor_job(handler.shutdown, None)
    return True

class ChronicleHandler(threading.Thread):
    """Handle database connections and writing."""

    def __init__(self, hass, db_url, table_name, chunk_interval, compress_after):
        """Initialize the handler."""
        threading.Thread.__init__(self)
        self.hass = hass
        self.db_url = db_url
        self.table_name = table_name
        self.chunk_interval = chunk_interval
        self.compress_after = compress_after
        
        self.queue = []
        self.lock = threading.Lock()
        self.running = True
        self.daemon = True
        
        self._engine = None

    def run(self):
        """Thread main loop."""
        self._connect()
        
        while self.running:
            time.sleep(FLUSH_INTERVAL)
            self._flush()

    def event_listener(self, event: Event):
        """Listen for new state change events."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if new_state is None:
            return

        # Simple filter for now (can be enhanced with options)
        # if not self.entity_filter(entity_id): return

        try:
            state_val = float(new_state.state)
        except (ValueError, TypeError):
            state_val = None

        data = {
            "time": new_state.last_updated,
            "entity_id": entity_id,
            "state": new_state.state,
            "attributes": json.dumps(dict(new_state.attributes), default=str),
            "value": state_val,
        }

        with self.lock:
            self.queue.append(data)
            
        if len(self.queue) >= BATCH_SIZE:
            self._flush()

    def _connect(self):
        """Connect to the database."""
        try:
            self._engine = create_engine(self.db_url)
            _LOGGER.info("Connected to TimescaleDB")
        except Exception as e:
            _LOGGER.error(f"Error connecting to database: {e}")

    def init_db(self):
        """Initialize database table, hypertable, and compression."""
        self._connect()
        if not self._engine:
            return

        with self._engine.connect() as conn:
            try:
                # Create Table
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {self.table_name} (
                        time TIMESTAMPTZ NOT NULL,
                        entity_id TEXT NOT NULL,
                        state TEXT,
                        attributes JSONB,
                        value DOUBLE PRECISION
                    );
                """))
                
                # Convert to Hypertable
                try:
                    conn.execute(text(f"SELECT create_hypertable('{self.table_name}', 'time', chunk_time_interval => INTERVAL '{self.chunk_interval}', if_not_exists => TRUE);"))
                except Exception as e:
                    _LOGGER.warning(f"Hypertable creation warning (might already exist): {e}")

                # Create Index
                conn.execute(text(f"""
                    CREATE INDEX IF NOT EXISTS {self.table_name}_entity_time_idx 
                    ON {self.table_name} (entity_id, time DESC);
                """))

                # Enable Compression
                try:
                    conn.execute(text(f"""
                        ALTER TABLE {self.table_name} SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'entity_id',
                            timescaledb.compress_orderby = 'time DESC'
                        );
                    """))
                except Exception:
                    pass # Already enabled

                # Add Compression Policy
                try:
                    conn.execute(text(f"SELECT add_compression_policy('{self.table_name}', INTERVAL '{self.compress_after}', if_not_exists => TRUE);"))
                except Exception as e:
                    _LOGGER.warning(f"Compression policy warning: {e}")
                    
                conn.commit()
                _LOGGER.info(f"Chronicle database initialized: {self.table_name}, chunk: {self.chunk_interval}, compress: {self.compress_after}")

            except Exception as e:
                _LOGGER.error(f"Error initializing database: {e}")

    def _flush(self):
        """Flush the queue to the database."""
        with self.lock:
            if not self.queue:
                return
            batch = list(self.queue)
            self.queue = []

        if not self._engine:
            self._connect()

        if not self._engine:
            return

        try:
            # Use raw connection for performance with execute_values
            with self._engine.raw_connection() as conn:
                with conn.cursor() as cursor:
                    sql = f"""
                        INSERT INTO {self.table_name} (time, entity_id, state, attributes, value)
                        VALUES %s
                    """
                    values = [
                        (x['time'], x['entity_id'], x['state'], x['attributes'], x['value'])
                        for x in batch
                    ]
                    execute_values(cursor, sql, values)
                conn.commit()
            
        except Exception as e:
            _LOGGER.error(f"Error inserting batch: {e}")
            # Retry logic could be added here

    def shutdown(self, event):
        """Shutdown the handler."""
        self.running = False
        self._flush()
        if self._engine:
            self._engine.dispose()
