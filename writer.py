"""Database writer for Scribe."""
import logging
import threading
import time
import json
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from psycopg2.extras import execute_values

from .const import (
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
)

_LOGGER = logging.getLogger(__name__)

BATCH_SIZE = 100
FLUSH_INTERVAL = 5

class ScribeWriter(threading.Thread):
    """Handle database connections and writing."""

    def __init__(self, hass, db_url, chunk_interval, compress_after, record_states, record_events, batch_size, flush_interval, table_name_states, table_name_events):
        """Initialize the writer."""
        threading.Thread.__init__(self)
        self.hass = hass
        self.db_url = db_url
        self.chunk_interval = chunk_interval
        self.compress_after = compress_after
        self.record_states = record_states
        self.record_events = record_events
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.table_name_states = table_name_states
        self.table_name_events = table_name_events
        
        # Stats
        self._events_written = 0
        self._last_write_duration = 0.0
        self._connected = False
        self._last_error = None
        
        self._queue = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        
        self.running = True
        self.daemon = True
        
        self._engine = None

    def run(self):
        """Thread main loop."""
        self._connect()
        
        while self.running:
            time.sleep(self.flush_interval)
            self._flush()

    def enqueue(self, data):
        """Add data to the queue."""
        with self.lock:
            self.queue.append(data)
            
        if len(self.queue) >= self.batch_size:
            self._flush()

    def _connect(self):
        """Connect to the database."""
        try:
            self._engine = create_engine(self.db_url, pool_size=10, max_overflow=20)
            _LOGGER.info("Connected to TimescaleDB")
        except Exception as e:
            _LOGGER.error(f"Error connecting to database: {e}")

    def init_db(self):
        """Initialize database tables, hypertables, and compression."""
        self._connect()
        if not self._engine:
            return

        with self._engine.connect() as conn:
            # States Table
            if self.record_states:
                try:
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS {self.table_name_states} (
                            time TIMESTAMPTZ NOT NULL,
                            entity_id TEXT NOT NULL,
                            state TEXT,
                            value DOUBLE PRECISION,
                            attributes JSONB
                        );
                    """))
                    conn.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS {self.table_name_states}_entity_time_idx 
                        ON {self.table_name_states} (entity_id, time DESC);
                    """))
                    self._init_hypertable(conn, self.table_name_states, "entity_id")
                except Exception as e:
                    _LOGGER.error(f"Error creating states table: {e}")

            # Events Table
            if self.record_events:
                try:
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS {self.table_name_events} (
                            time TIMESTAMPTZ NOT NULL,
                            event_type TEXT NOT NULL,
                            event_data JSONB,
                            origin TEXT,
                            context_id TEXT,
                            context_user_id TEXT,
                            context_parent_id TEXT
                        );
                    """))
                    conn.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS {self.table_name_events}_type_time_idx 
                        ON {self.table_name_events} (event_type, time DESC);
                    """))
                    self._init_hypertable(conn, self.table_name_events, "event_type")
                except Exception as e:
                    _LOGGER.error(f"Error creating events table: {e}")
            
            conn.commit()

    def _init_hypertable(self, conn, table_name, segment_by):
        """Initialize hypertable and compression for a table."""
        try:
            # Convert to Hypertable
            try:
                conn.execute(text(f"SELECT create_hypertable('{table_name}', 'time', chunk_time_interval => INTERVAL '{self.chunk_interval}', if_not_exists => TRUE);"))
            except Exception:
                pass 

            # Enable Compression
            try:
                conn.execute(text(f"""
                    ALTER TABLE {table_name} SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = '{segment_by}',
                        timescaledb.compress_orderby = 'time DESC'
                    );
                """))
            except Exception:
                pass 

            # Add Compression Policy
            try:
                conn.execute(text(f"SELECT add_compression_policy('{table_name}', INTERVAL '{self.compress_after}', if_not_exists => TRUE);"))
            except Exception:
                pass
                
            _LOGGER.info(f"Initialized {table_name} (chunk: {self.chunk_interval}, compress: {self.compress_after})")
            
        except Exception as e:
            _LOGGER.error(f"Error initializing {table_name}: {e}")

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
            # Separate batches
            states_data = [x for x in batch if x['type'] == 'state']
            events_data = [x for x in batch if x['type'] == 'event']

            with self._engine.raw_connection() as conn:
                with conn.cursor() as cursor:
                    
                    # Insert States
                    if states_data:
                        sql = f"""
                            INSERT INTO {self.table_name_states} (time, entity_id, state, value, attributes)
                        conn.execute(
                            text(f"INSERT INTO {self.table_name_states} (time, entity_id, state, value, attributes) VALUES (:time, :entity_id, :state, :value, :attributes)"),
                            states_data
                        )
                    if events_data:
                        conn.execute(
                            text(f"INSERT INTO {self.table_name_events} (time, event_type, event_data, origin, context_id, context_user_id, context_parent_id) VALUES (:time, :event_type, :event_data, :origin, :context_id, :context_user_id, :context_parent_id)"),
                            events_data
                        )
            
            duration = time.time() - start_time
            with self._lock:
                self._events_written += len(batch)
                self._last_write_duration = duration
                self._connected = True
                self._last_error = None
                
            _LOGGER.debug(f"Flushed {len(batch)} items in {duration:.3f}s")
            
        except Exception as e:
            _LOGGER.error(f"Error flushing buffer: {e}")
            with self._lock:
                self._connected = False
                self._last_error = str(e)
            # Put items back in queue? For now, we drop them to avoid memory explosion
            # But in a real robust system, we might retry.
            # For Scribe, we prioritize stability of HA over data loss.
    def shutdown(self, event):
        """Shutdown the handler."""
        self.running = False
        self._flush()
        if self._engine:
            self._engine.dispose()
