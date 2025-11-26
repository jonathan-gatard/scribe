"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements an asyncio-based writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.
"""
import logging
import asyncio
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

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
                _LOGGER.debug(f"Creating AsyncEngine for {self.db_url.split('@')[-1]} with pool_size=10, max_overflow=20")
                self._engine = create_async_engine(
                    self.db_url, 
                    pool_size=10, 
                    max_overflow=20,
                    echo=False # We handle our own logging
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
        Wait, if this is called from event listener, it is in the main thread.
        We don't need a lock for simple list append in asyncio if we are single-threaded event loop,
        BUT _flush is async and might yield. So we should be careful.
        Actually, in asyncio, context switches only happen at await. 
        So `self._queue.append` is atomic relative to other tasks.
        """
        if len(self._queue) >= self.max_queue_size:
            self._dropped_events += 1
            if self._dropped_events % 100 == 1:
                _LOGGER.warning(f"Queue full ({len(self._queue)}), dropping event. Total dropped: {self._dropped_events}")
            return
        
        self._queue.append(data)
        
        # Trigger flush if batch size reached
        if len(self._queue) >= self.batch_size:
            _LOGGER.debug(f"Batch size reached ({len(self._queue)} >= {self.batch_size}), triggering flush")
            asyncio.create_task(self._flush())

    async def init_db(self):
        """Initialize database tables."""
        _LOGGER.debug("Initializing database...")
        if not self._engine:
            return

        try:
            async with self._engine.begin() as conn:
                if self.record_states:
                    await self._init_states_table(conn)
                if self.record_events:
                    await self._init_events_table(conn)

            # Hypertable & Compression (separate transactions for safety)
            if self.record_states:
                async with self._engine.begin() as conn:
                    await self._init_hypertable(conn, self.table_name_states, "entity_id")
            
            if self.record_events:
                async with self._engine.begin() as conn:
                    await self._init_hypertable(conn, self.table_name_events, "event_type")
                    
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

    async def _init_hypertable(self, conn, table_name, segment_by):
        """Initialize hypertable and compression."""
        try:
            _LOGGER.debug(f"Converting {table_name} to hypertable...")
            await conn.execute(text(f"SELECT create_hypertable('{table_name}', 'time', chunk_time_interval => INTERVAL '{self.chunk_interval}', if_not_exists => TRUE);"))
        except Exception as e:
            _LOGGER.debug(f"Hypertable creation failed (might not be TimescaleDB or already exists): {e}")

        try:
            _LOGGER.debug(f"Enabling compression for {table_name}...")
            await conn.execute(text(f"""
                ALTER TABLE {table_name} SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = '{segment_by}',
                    timescaledb.compress_orderby = 'time DESC'
                );
            """))
        except Exception as e:
             _LOGGER.debug(f"Compression enable failed: {e}")

        try:
            _LOGGER.debug(f"Adding compression policy for {table_name}...")
            await conn.execute(text(f"SELECT add_compression_policy('{table_name}', INTERVAL '{self.compress_after}', if_not_exists => TRUE);"))
        except Exception as e:
            _LOGGER.debug(f"Compression policy failed: {e}")

    async def _flush(self):
        """Flush the queue to the database."""
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

    async def get_db_stats(self):
        """Fetch database statistics."""
        stats = {}
        if not self._engine:
            return stats
            
        try:
            async with self._engine.connect() as conn:
                # States Table Stats
                if self.record_states:
                    try:
                        res = await conn.execute(text(f"SELECT hypertable_size('{self.table_name_states}')"))
                        stats["states_size_bytes"] = res.scalar()
                        
                        # Try newer format first, fall back to simpler query
                        try:
                            res = await conn.execute(text(f"SELECT total_chunks, compressed_total_bytes, uncompressed_total_bytes FROM hypertable_compression_stats('{self.table_name_states}')"))
                            row = res.fetchone()
                            if row:
                                stats["states_total_chunks"] = row[0]
                                stats["states_compressed_total_bytes"] = row[1] or 0
                                stats["states_uncompressed_total_bytes"] = row[2] or 0
                        except Exception:
                            # Older TimescaleDB version - just get basic size
                            pass
                    except Exception as e:
                        _LOGGER.debug(f"Failed to get states stats: {e}")

                # Events Table Stats
                if self.record_events:
                    try:
                        res = await conn.execute(text(f"SELECT hypertable_size('{self.table_name_events}')"))
                        stats["events_size_bytes"] = res.scalar()
                        
                        # Try newer format first, fall back to simpler query
                        try:
                            res = await conn.execute(text(f"SELECT total_chunks, compressed_total_bytes, uncompressed_total_bytes FROM hypertable_compression_stats('{self.table_name_events}')"))
                            row = res.fetchone()
                            if row:
                                stats["events_total_chunks"] = row[0]
                                stats["events_compressed_total_bytes"] = row[1] or 0
                                stats["events_uncompressed_total_bytes"] = row[2] or 0
                        except Exception:
                            # Older TimescaleDB version - just get basic size
                            pass
                    except Exception as e:
                        _LOGGER.debug(f"Failed to get events stats: {e}")
                        
        except Exception as e:
            _LOGGER.error(f"Error fetching stats: {e}")
            
        return stats

    @property
    def running(self):
        return self._running
