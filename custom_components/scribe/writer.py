"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements a threaded writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.
"""
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
    """Handle database connections and writing.
    
    This class runs as a daemon thread. It maintains a queue of events to be written.
    Data is flushed to the database when the queue reaches BATCH_SIZE or when
    FLUSH_INTERVAL seconds have passed.
    """

    def __init__(self, hass, db_url, chunk_interval, compress_after, record_states, record_events, batch_size, flush_interval, max_queue_size, buffer_on_failure, table_name_states, table_name_events):
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
        self.max_queue_size = max_queue_size
        self.buffer_on_failure = buffer_on_failure
        self.table_name_states = table_name_states
        self.table_name_events = table_name_events
        
        # Stats for sensors
        self._states_written = 0
        self._events_written = 0
        self._last_write_duration = 0.0
        self._connected = False
        self._last_error = None
        self._dropped_events = 0
        
        # Thread safety
        self._queue = []
        self._lock = threading.Lock() # Protects access to self._queue
        self._stop_event = threading.Event()
        self._thread = None
        
        self.running = True
        self.daemon = True # Thread dies when main process dies
        
        self._engine = None

    def run(self):
        """Thread main loop.
        
        Continuously checks if it's time to flush the queue based on the timer.
        """
        self._connect()
        
        while self.running:
            time.sleep(self.flush_interval)
            self._flush()

    def enqueue(self, data):
        """Add data to the queue.
        
        This method is called from the main event loop, so it must be fast and thread-safe.
        If the queue size exceeds the batch size, a flush is triggered immediately.
        """
        with self._lock:
            if len(self._queue) >= self.max_queue_size:
                self._dropped_events += 1
                if self._dropped_events % 100 == 1:
                    _LOGGER.warning(f"Queue full ({len(self._queue)}), dropping event. Total dropped: {self._dropped_events}")
                return
            
            self._queue.append(data)
            if len(self._queue) % 10 == 0:
                _LOGGER.debug(f"Enqueued item. Queue size: {len(self._queue)}")
            
        if len(self._queue) >= self.batch_size:
            self._flush()

    def _connect(self):
        """Connect to the database."""
        try:
            # Create SQLAlchemy engine with connection pooling
            self._engine = create_engine(self.db_url, pool_size=10, max_overflow=20)
            _LOGGER.info("Connected to TimescaleDB")
        except Exception as e:
            _LOGGER.error(f"Error connecting to database: {e}")

    def init_db(self):
        """Initialize database tables, hypertables, and compression.
        
        This is called once during startup. It creates the necessary tables if they
        don't exist and configures TimescaleDB specific features (hypertables, compression).
        """
        self._connect()
        if not self._engine:
            return

        with self._engine.connect() as conn:
            # States Table
            if self.record_states:
                try:
                    # Create standard table
                    conn.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS {self.table_name_states} (
                            time TIMESTAMPTZ NOT NULL,
                            entity_id TEXT NOT NULL,
                            state TEXT,
                            value DOUBLE PRECISION,
                            attributes JSONB
                        );
                    """))
                    # Create index for fast querying by entity and time
                    conn.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS {self.table_name_states}_entity_time_idx 
                        ON {self.table_name_states} (entity_id, time DESC);
                    """))
                    # Convert to Hypertable
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
        """Initialize hypertable and compression for a table.
        
        TimescaleDB features:
        - Hypertable: Partitions data by time (chunk_interval).
        - Compression: Compresses old data to save space (compress_after).
        """
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
        """Flush the queue to the database.
        
        This method takes all items currently in the queue and writes them to the DB
        in a single transaction.
        """
        # Atomically swap the queue with an empty one
        with self._lock:
            if not self._queue:
                return
            batch = list(self._queue)
            self._queue = []

        if not self._engine:
            self._connect()
            if not self._engine:
                # Re-queue batch if no connection
                with self._lock:
                    if self.buffer_on_failure:
                        self._queue = batch + self._queue
                    else:
                        self._dropped_events += len(batch)
                        _LOGGER.warning(f"Database not connected and buffering is disabled. Dropped {len(batch)} events.")
                return

        start_time = time.time()
        try:
            # Separate batches by type
            states_data = [x for x in batch if x['type'] == 'state']
            events_data = [x for x in batch if x['type'] == 'event']

            with self._engine.connect() as conn:
                with conn.begin(): # Start transaction
                    if states_data:
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
            
            # Update stats
            with self._lock:
                self._states_written += len(states_data)
                self._events_written += len(events_data)
                self._last_write_duration = duration
                self._connected = True
                self._last_error = None
                
            _LOGGER.debug(f"Flushed {len(batch)} items in {duration:.3f}s")
            
        except Exception as e:
            _LOGGER.error(f"Error inserting batch: {e}")
            with self._lock:
                self._connected = False
                self._last_error = str(e)
                
                if not self.buffer_on_failure:
                    self._dropped_events += len(batch)
                    _LOGGER.warning(f"Database write failed and buffering is disabled. Dropped {len(batch)} events. Error: {e}")
                    return

                # Re-queue batch on failure
                # Prepend to queue to maintain order (mostly)
                # Check max size to avoid infinite growth if DB is down for a long time
                remaining_space = self.max_queue_size - len(self._queue)
                if remaining_space > 0:
                    self._queue = batch[:remaining_space] + self._queue
                    if len(batch) > remaining_space:
                        self._dropped_events += (len(batch) - remaining_space)
                        _LOGGER.warning(f"Buffer full during retry, dropped {len(batch) - remaining_space} events")

    def get_db_stats(self):
        """Fetch database statistics.
        
        Queries TimescaleDB specific functions to get size and compression stats.
        Used by the coordinator to update sensors.
        """
        stats = {}
        if not self._engine:
            return stats
            
        try:
            with self._engine.connect() as conn:
                # States Table Stats
                if self.record_states:
                    try:
                        # Total Size (Disk Usage)
                        res = conn.execute(text(f"SELECT hypertable_size('{self.table_name_states}')")).scalar()
                        stats["states_size_bytes"] = res
                        
                        # Compression Stats
                        res = conn.execute(text(f"SELECT total_chunks, compressed_chunks, compressed_total_bytes, uncompressed_total_bytes FROM hypertable_compression_stats('{self.table_name_states}')")).fetchone()
                        if res:
                            stats["states_total_chunks"] = res[0]
                            stats["states_compressed_chunks"] = res[1]
                            stats["states_compressed_total_bytes"] = res[2] or 0
                            stats["states_uncompressed_total_bytes"] = res[3] or 0
                    except Exception:
                        pass # Likely not a hypertable or no compression yet

                # Events Table Stats
                if self.record_events:
                    try:
                        # Total Size (Disk Usage)
                        res = conn.execute(text(f"SELECT hypertable_size('{self.table_name_events}')")).scalar()
                        stats["events_size_bytes"] = res
                        
                        # Compression Stats
                        res = conn.execute(text(f"SELECT total_chunks, compressed_chunks, compressed_total_bytes, uncompressed_total_bytes FROM hypertable_compression_stats('{self.table_name_events}')")).fetchone()
                        if res:
                            stats["events_total_chunks"] = res[0]
                            stats["events_compressed_chunks"] = res[1]
                            stats["events_compressed_total_bytes"] = res[2] or 0
                            stats["events_uncompressed_total_bytes"] = res[3] or 0
                    except Exception:
                        pass

        except Exception as e:
            _LOGGER.error(f"Error fetching stats: {e}")
            
        return stats
        
    def shutdown(self, event):
        """Shutdown the handler.
        
        Stops the thread and flushes any remaining data.
        """
        self.running = False
        self._flush()
        if self._engine:
            self._engine.dispose()
