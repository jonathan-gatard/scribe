"""Database writer for Scribe."""
import logging
import threading
import time
import json
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from psycopg2.extras import execute_values

from .models import Base, State, Event
from .const import (
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
)

_LOGGER = logging.getLogger(__name__)

BATCH_SIZE = 100
FLUSH_INTERVAL = 5

class ScribeWriter(threading.Thread):
    """Handle database connections and writing."""

    def __init__(self, hass, db_url, chunk_interval, compress_after, record_states, record_events, batch_size, flush_interval):
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
        
        self.queue = []
        self.lock = threading.Lock()
        self.running = True
        self.daemon = True
        
        self._engine = None
        self._session = None

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

        # Create tables
        Base.metadata.create_all(self._engine)

        with self._engine.connect() as conn:
            # States Table
            if self.record_states:
                self._init_hypertable(conn, "states", "entity_id")

            # Events Table
            if self.record_events:
                self._init_hypertable(conn, "events", "event_type")

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
                        sql = """
                            INSERT INTO states (time, entity_id, state, value, attributes)
                            VALUES %s
                        """
                        values = [
                            (x['time'], x['entity_id'], x['state'], x['value'], x['attributes'])
                            for x in states_data
                        ]
                        execute_values(cursor, sql, values)

                    # Insert Events
                    if events_data:
                        sql = """
                            INSERT INTO events (time, event_type, event_data, origin, context_id, context_user_id, context_parent_id)
                            VALUES %s
                        """
                        values = [
                            (x['time'], x['event_type'], x['event_data'], x['origin'], x['context_id'], x['context_user_id'], x['context_parent_id'])
                            for x in events_data
                        ]
                        execute_values(cursor, sql, values)
                        
                conn.commit()
            
        except Exception as e:
            _LOGGER.error(f"Error inserting batch: {e}")

    def shutdown(self, event):
        """Shutdown the handler."""
        self.running = False
        self._flush()
        if self._engine:
            self._engine.dispose()
