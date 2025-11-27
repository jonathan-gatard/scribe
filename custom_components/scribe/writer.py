"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements an asyncio-based writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.
"""
import logging
import asyncio
import json
import ssl
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _create_ssl_context() -> ssl.SSLContext:
    """Create and configure SSL context in executor thread.
    
    asyncpg calls ssl.load_cert_chain() synchronously when establishing SSL connections.
    By creating the SSLContext here (in an executor thread) and passing it to asyncpg,
    we avoid blocking Home Assistant's event loop.
    
    This function must be run via hass.async_add_executor_job().
    
    Returns:
        Configured SSLContext ready to be used by asyncpg
    """
    _LOGGER.debug("Creating SSL context in executor thread...")
    
    # Create SSL context
    ssl_context = ssl.create_default_context()
    
    # Load system CA certificates
    try:
        ssl_context.load_default_certs()
        _LOGGER.debug("Loaded system CA certificates")
    except Exception as e:
        _LOGGER.debug("Could not load system CA certificates: %s", e)
    
    # Load PostgreSQL client certificates from standard locations
    # asyncpg checks these paths: /.postgresql/ (containers) and ~/.postgresql/
    cert_locations = [
        (Path("/.postgresql/postgresql.crt"), Path("/.postgresql/postgresql.key")),
        (Path.home() / ".postgresql" / "postgresql.crt", Path.home() / ".postgresql" / "postgresql.key"),
    ]
    
    for cert_path, key_path in cert_locations:
        if cert_path.exists():
            try:
                key_file = str(key_path) if key_path.exists() else None
                _LOGGER.debug("Loading PostgreSQL client certificate from %s", cert_path)
                ssl_context.load_cert_chain(str(cert_path), key_file)
                break  # Only load from first found location
            except Exception as e:
                _LOGGER.debug("Could not load cert chain from %s: %s", cert_path, e)
    
    # Load CA certificate for server verification
    for ca_path in [Path("/.postgresql/root.crt"), Path.home() / ".postgresql" / "root.crt"]:
        if ca_path.exists():
            try:
                _LOGGER.debug("Loading CA certificate from %s", ca_path)
                ssl_context.load_verify_locations(str(ca_path))
                break  # Only load from first found location
            except Exception as e:
                _LOGGER.debug("Could not load CA cert from %s: %s", ca_path, e)
    
    _LOGGER.debug("SSL context created successfully")
    return ssl_context

class ScribeWriter:
    """Handle database connections and writing.
    
    This class runs as an asyncio task. It maintains a queue of events to be written.
    Data is flushed to the database when the queue reaches BATCH_SIZE or when
    FLUSH_INTERVAL seconds have passed.
    """

    def __init__(
        self, 
        hass: HomeAssistant, 
        db_url: str, 
        chunk_interval: str, 
        compress_after: str, 
        record_states: bool, 
        record_events: bool, 
        batch_size: int, 
        flush_interval: int, 
        max_queue_size: int, 
        buffer_on_failure: bool, 
        table_name_states: str, 
        table_name_events: str,
        use_ssl: bool = False,
        engine: Any = None
    ):
        """Initialize the writer."""
        self.hass = hass
        
        # Ensure db_url uses asyncpg
        if "postgresql://" in db_url and "postgresql+asyncpg://" not in db_url:
            self.db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
        else:
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
        self.use_ssl = use_ssl
        
        # Stats for sensors
        self._states_written = 0
        self._events_written = 0
        self._last_write_duration = 0.0
        self._connected = False
        self._last_error = None
        self._dropped_events = 0
        
        # Queue
        self._queue: List[Dict[str, Any]] = []
        self._queue_lock = asyncio.Lock()
        self._flush_pending = False  # Prevent multiple flush tasks
        
        self._engine = engine
        self._task = None
        self._running = False

    async def start(self):
        """Start the writer task."""
        if self._running:
            return
            
        _LOGGER.debug("Starting ScribeWriter...")
        self._running = True
        
        # Create Engine
        if not self._engine:
            try:
                _LOGGER.debug(f"Creating AsyncEngine for {self.db_url.split('@')[-1]} (attempt 1)")
                
                # Clean URL - remove sslmode parameter as asyncpg doesn't support it in URL
                clean_url = self._clean_db_url(self.db_url)
                
                if self.use_ssl:
                    # Create SSL context in executor to avoid blocking the event loop
                    # asyncpg calls ssl.load_cert_chain() synchronously when it detects
                    # certificates. By providing our own pre-configured SSL context,
                    # we prevent asyncpg from doing blocking I/O in the event loop.
                    _LOGGER.debug("SSL enabled, creating SSL context in executor...")
                    ssl_context = await self.hass.async_add_executor_job(_create_ssl_context)
                    connect_args = {"ssl": ssl_context}
                else:
                    # Disable SSL explicitly to prevent asyncpg from auto-detecting
                    # certificates and doing blocking I/O
                    _LOGGER.debug("SSL disabled (default)")
                    connect_args = {"ssl": False}
                
                # Create engine
                self._engine = create_async_engine(
                    clean_url,
                    pool_size=10,
                    max_overflow=20,
                    echo=False,
                    connect_args=connect_args
                )
                
                _LOGGER.debug("AsyncEngine created successfully")
            except Exception as e:
                print(f"DEBUG: Failed to create engine: {e}")
                _LOGGER.error(f"Failed to create engine: {e}", exc_info=True)
                return

        # Initialize DB
        _LOGGER.debug("Starting database initialization...")
        await self.init_db()
        _LOGGER.debug("Database initialization completed")
        
        # Start Loop
        self._task = asyncio.create_task(self._run())
        _LOGGER.info("ScribeWriter started successfully")

    def _clean_db_url(self, url: str) -> str:
        """Remove sslmode parameter from URL as asyncpg handles it via connect_args."""
        import re
        # Remove sslmode=xxx from URL (handles both ?sslmode= and &sslmode=)
        cleaned = re.sub(r'[?&]sslmode=[^&]*', '', url)
        # Fix URL if we removed the first parameter (? becomes nothing)
        cleaned = re.sub(r'\?&', '?', cleaned)
        # Remove trailing ? if no parameters left
        cleaned = re.sub(r'\?$', '', cleaned)
        return cleaned

    async def stop(self):
        """Stop the writer task."""
        _LOGGER.debug("Stopping ScribeWriter...")
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        # Final flush
        await self._flush()
        
        if self._engine:
            await self._engine.dispose()
            _LOGGER.debug("Engine disposed")

    async def _run(self):
        """Main loop."""
        _LOGGER.debug("ScribeWriter loop started")
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Error in writer loop: {e}")

    def enqueue(self, data: Dict[str, Any]):
        """Add data to the queue.
        
        This is called from the main loop, so it shouldn't block.
        We use a simple list append here since we are in the main thread (asyncio).
        Context switches only happen at await, so append is atomic.
        """
        if len(self._queue) >= self.max_queue_size:
            self._dropped_events += 1
            if self._dropped_events % 100 == 1:
                _LOGGER.warning(f"Queue full ({len(self._queue)}), dropping event. Total dropped: {self._dropped_events}")
            return
        
        self._queue.append(data)
        
        # Trigger flush if batch size reached (but only if no flush is already pending)
        if len(self._queue) >= self.batch_size and not self._flush_pending:
            self._flush_pending = True
            _LOGGER.debug(f"Batch size reached ({len(self._queue)} >= {self.batch_size}), triggering flush")
            asyncio.create_task(self._flush())

    async def init_db(self):
        """Initialize database tables."""
        _LOGGER.debug("Initializing database...")
        if not self._engine:
            return

        try:
            # Create tables
            async with self._engine.begin() as conn:
                if self.record_states:
                    await self._init_states_table(conn)
                if self.record_events:
                    await self._init_events_table(conn)

            # Hypertable & Compression (each operation in its own transaction)
            if self.record_states:
                await self._init_hypertable(self.table_name_states, "entity_id")
            
            if self.record_events:
                await self._init_hypertable(self.table_name_events, "event_type")
                    
            _LOGGER.info("Database initialized successfully")
            self._connected = True

        except Exception as e:
            _LOGGER.error(f"Error initializing database: {e}")
            self._connected = False

    async def _init_states_table(self, conn):
        """Initialize states table."""
        _LOGGER.debug(f"Creating table {self.table_name_states} if not exists")
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name_states} (
                time TIMESTAMPTZ NOT NULL,
                entity_id TEXT NOT NULL,
                state TEXT,
                value DOUBLE PRECISION,
                attributes JSONB
            );
        """))
        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS {self.table_name_states}_entity_time_idx 
            ON {self.table_name_states} (entity_id, time DESC);
        """))

    async def _init_events_table(self, conn):
        """Initialize events table."""
        _LOGGER.debug(f"Creating table {self.table_name_events} if not exists")
        await conn.execute(text(f"""
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
        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS {self.table_name_events}_type_time_idx 
            ON {self.table_name_events} (event_type, time DESC);
        """))

    async def _init_hypertable(self, table_name, segment_by):
        """Initialize hypertable and compression.
        
        Each operation is done in its own transaction to avoid
        'transaction aborted' errors when one operation fails.
        """
        
        # Convert to hypertable
        try:
            _LOGGER.debug(f"Converting {table_name} to hypertable...")
            async with self._engine.begin() as op_conn:
                await op_conn.execute(text(f"SELECT create_hypertable('{table_name}', 'time', chunk_time_interval => INTERVAL '{self.chunk_interval}', if_not_exists => TRUE);"))
        except Exception as e:
            _LOGGER.debug(f"Hypertable creation failed (might not be TimescaleDB or already exists): {e}")

        # Enable compression
        try:
            _LOGGER.debug(f"Enabling compression for {table_name}...")
            async with self._engine.begin() as op_conn:
                await op_conn.execute(text(f"""
                    ALTER TABLE {table_name} SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = '{segment_by}',
                        timescaledb.compress_orderby = 'time DESC'
                    );
                """))
        except Exception as e:
             _LOGGER.debug(f"Compression enable failed: {e}")

        # Add compression policy
        try:
            _LOGGER.debug(f"Adding compression policy for {table_name}...")
            async with self._engine.begin() as op_conn:
                await op_conn.execute(text(f"SELECT add_compression_policy('{table_name}', INTERVAL '{self.compress_after}', if_not_exists => TRUE);"))
        except Exception as e:
            _LOGGER.debug(f"Compression policy failed: {e}")

    async def _flush(self):
        """Flush the queue to the database."""
        self._flush_pending = False  # Reset flag immediately
        
        if not self._queue:
            return

        # Swap queue
        batch = self._queue
        self._queue = []
        
        _LOGGER.debug(f"Flushing {len(batch)} items...")
        start_time = time.time()
        
        try:
            states_data = [x for x in batch if x['type'] == 'state']
            events_data = [x for x in batch if x['type'] == 'event']
            
            async with self._engine.begin() as conn:
                if states_data:
                    _LOGGER.debug(f"Inserting {len(states_data)} states...")
                    await conn.execute(
                        text(f"INSERT INTO {self.table_name_states} (time, entity_id, state, value, attributes) VALUES (:time, :entity_id, :state, :value, :attributes)"),
                        states_data
                    )
                if events_data:
                    _LOGGER.debug(f"Inserting {len(events_data)} events...")
                    await conn.execute(
                        text(f"INSERT INTO {self.table_name_events} (time, event_type, event_data, origin, context_id, context_user_id, context_parent_id) VALUES (:time, :event_type, :event_data, :origin, :context_id, :context_user_id, :context_parent_id)"),
                        events_data
                    )
            
            duration = time.time() - start_time
            self._states_written += len(states_data)
            self._events_written += len(events_data)
            self._last_write_duration = duration
            self._connected = True
            self._last_error = None
            _LOGGER.debug(f"Flush complete in {duration:.3f}s")

        except Exception as e:
            _LOGGER.error(f"Error flushing batch: {e}", exc_info=True)
            self._connected = False
            self._last_error = str(e)
            
            if self.buffer_on_failure:
                _LOGGER.warning(f"Buffering {len(batch)} items due to failure. Current queue size: {len(self._queue)}")
                # Prepend back to queue
                self._queue = batch + self._queue
                
                # Check max size
                if len(self._queue) > self.max_queue_size:
                    dropped = len(self._queue) - self.max_queue_size
                    self._queue = self._queue[-self.max_queue_size:]
                    self._dropped_events += dropped
                    _LOGGER.error(f"Buffer full! Dropped {dropped} oldest events. Queue size: {len(self._queue)}")
            else:
                self._dropped_events += len(batch)
                _LOGGER.warning(f"Dropped {len(batch)} items (buffering disabled)")

    async def get_db_stats(self, stats_type: str = "all"):
        """Fetch database statistics using TimescaleDB chunks view.
        
        Args:
            stats_type: Type of stats to fetch - "chunk", "size", or "all"
        
        Uses 4 separate queries:
        - States chunk stats (counts)
        - States size stats (bytes)
        - Events chunk stats (counts)
        - Events size stats (bytes)
        """
        stats = {}
        if not self._engine:
            return stats
        
        # =============================================
        # STATES TABLE STATISTICS
        # =============================================
        if self.record_states:
            # Query 1: States chunk counts
            if stats_type in ("chunk", "all"):
                try:
                    async with self._engine.connect() as conn:
                        res = await conn.execute(text(f"""
                            SELECT 
                                COUNT(*) AS total_chunks,
                                SUM(CASE WHEN is_compressed THEN 1 ELSE 0 END) AS compressed_chunks,
                                SUM(CASE WHEN NOT is_compressed THEN 1 ELSE 0 END) AS uncompressed_chunks
                            FROM timescaledb_information.chunks
                            WHERE hypertable_name = '{self.table_name_states}'
                        """))
                        row = res.fetchone()
                        if row:
                            stats["states_total_chunks"] = row[0] or 0
                            stats["states_compressed_chunks"] = row[1] or 0
                            stats["states_uncompressed_chunks"] = row[2] or 0
                except Exception as e:
                    _LOGGER.debug(f"Failed to get states chunk stats: {e}")
            
            # Query 2: States size breakdown
            if stats_type in ("size", "all"):
                try:
                    async with self._engine.connect() as conn:
                        res = await conn.execute(text(f"""
                            SELECT 
                                SUM(pg_total_relation_size(chunk_schema || '.' || chunk_name)) AS total_size,
                                SUM(CASE WHEN is_compressed 
                                    THEN pg_total_relation_size(chunk_schema || '.' || chunk_name) 
                                    ELSE 0 END) AS compressed_size,
                                SUM(CASE WHEN NOT is_compressed 
                                    THEN pg_total_relation_size(chunk_schema || '.' || chunk_name) 
                                    ELSE 0 END) AS uncompressed_size
                            FROM timescaledb_information.chunks
                            WHERE hypertable_name = '{self.table_name_states}'
                        """))
                        row = res.fetchone()
                        if row:
                            stats["states_total_size"] = row[0] or 0
                            stats["states_compressed_size"] = row[1] or 0
                            stats["states_uncompressed_size"] = row[2] or 0
                except Exception as e:
                    _LOGGER.debug(f"Failed to get states size stats: {e}")

        # =============================================
        # EVENTS TABLE STATISTICS
        # =============================================
        if self.record_events:
            # Query 3: Events chunk counts
            if stats_type in ("chunk", "all"):
                try:
                    async with self._engine.connect() as conn:
                        res = await conn.execute(text(f"""
                            SELECT 
                                COUNT(*) AS total_chunks,
                                SUM(CASE WHEN is_compressed THEN 1 ELSE 0 END) AS compressed_chunks,
                                SUM(CASE WHEN NOT is_compressed THEN 1 ELSE 0 END) AS uncompressed_chunks
                            FROM timescaledb_information.chunks
                            WHERE hypertable_name = '{self.table_name_events}'
                        """))
                        row = res.fetchone()
                        if row:
                            stats["events_total_chunks"] = row[0] or 0
                            stats["events_compressed_chunks"] = row[1] or 0
                            stats["events_uncompressed_chunks"] = row[2] or 0
                except Exception as e:
                    _LOGGER.debug(f"Failed to get events chunk stats: {e}")
            
            # Query 4: Events size breakdown
            if stats_type in ("size", "all"):
                try:
                    async with self._engine.connect() as conn:
                        res = await conn.execute(text(f"""
                            SELECT 
                                SUM(pg_total_relation_size(chunk_schema || '.' || chunk_name)) AS total_size,
                                SUM(CASE WHEN is_compressed 
                                    THEN pg_total_relation_size(chunk_schema || '.' || chunk_name) 
                                    ELSE 0 END) AS compressed_size,
                                SUM(CASE WHEN NOT is_compressed 
                                    THEN pg_total_relation_size(chunk_schema || '.' || chunk_name) 
                                    ELSE 0 END) AS uncompressed_size
                            FROM timescaledb_information.chunks
                            WHERE hypertable_name = '{self.table_name_events}'
                        """))
                        row = res.fetchone()
                        if row:
                            stats["events_total_size"] = row[0] or 0
                            stats["events_compressed_size"] = row[1] or 0
                            stats["events_uncompressed_size"] = row[2] or 0
                except Exception as e:
                    _LOGGER.debug(f"Failed to get events size stats: {e}")
            
        return stats

    @property
    def running(self):
        return self._running