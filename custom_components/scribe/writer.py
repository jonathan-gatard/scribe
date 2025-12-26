"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements an asyncio-based writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.
"""
import logging
import asyncio
import ssl
import time
from pathlib import Path
from typing import Any, Dict
from collections import deque
import json

from homeassistant.helpers.json import JSONEncoder

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _create_ssl_context(ssl_root_cert=None, ssl_cert_file=None, ssl_key_file=None) -> ssl.SSLContext:
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
    
    # Load PostgreSQL client certificates
    if ssl_cert_file and Path(ssl_cert_file).exists():
        try:
            _LOGGER.debug("Loading PostgreSQL client certificate from %s", ssl_cert_file)
            ssl_context.load_cert_chain(ssl_cert_file, ssl_key_file)
        except Exception as e:
            _LOGGER.error("Could not load cert chain from %s: %s", ssl_cert_file, e)
    
    # Load CA certificate for server verification
    if ssl_root_cert and Path(ssl_root_cert).exists():
        try:
            _LOGGER.debug("Loading CA certificate from %s", ssl_root_cert)
            ssl_context.load_verify_locations(ssl_root_cert)
        except Exception as e:
            _LOGGER.error("Could not load CA cert from %s: %s", ssl_root_cert, e)
    
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
        ssl_root_cert: str = None,
        ssl_cert_file: str = None,
        ssl_key_file: str = None,
        enable_areas: bool = True,
        enable_devices: bool = True,
        enable_table_areas: bool = True,
        enable_table_devices: bool = True,
        enable_table_entities: bool = True,
        enable_table_integrations: bool = True,
        enable_table_users: bool = True,
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
        self.ssl_root_cert = ssl_root_cert
        self.ssl_cert_file = ssl_cert_file
        self.ssl_key_file = ssl_key_file
        self.enable_table_areas = enable_table_areas
        self.enable_table_devices = enable_table_devices
        self.enable_table_entities = enable_table_entities
        self.enable_table_integrations = enable_table_integrations
        self.enable_table_users = enable_table_users
        
        # Stats for sensors
        self._states_written = 0
        self._events_written = 0
        self._last_write_duration = None
        self._connected = False
        self._last_error = None
        self._states_history = deque()
        self._events_history = deque()
        self._dropped_events = 0
        
        # Queue
        self._queue: deque = deque(maxlen=max_queue_size)
        self._queue: deque = deque(maxlen=max_queue_size)
        self._flush_pending = False  # Prevent multiple flush tasks
        
        self._engine = engine
        self._task = None
        self._running = False

    async def start(self):
        """Start the writer task."""
        try:
            if self._running:
                return
                
            _LOGGER.debug("Starting ScribeWriter...")
            self._running = True
            
            # Create Engine
            if not self._engine:
                try:
                    _LOGGER.debug(f"Creating AsyncEngine for {self.db_url.split('@')[-1]} (attempt 1)")
                    
                    if self.use_ssl:
                        # Resolve paths relative to HA config dir if they are relative
                        def resolve_path(path_str):
                            if not path_str:
                                return None
                            path = Path(path_str)
                            if not path.is_absolute():
                                return str(Path(self.hass.config.config_dir) / path)
                            return str(path)

                        root_cert = resolve_path(self.ssl_root_cert)
                        cert_file = resolve_path(self.ssl_cert_file)
                        key_file = resolve_path(self.ssl_key_file)

                        # Create SSL context in executor to avoid blocking the event loop
                        # We pass the resolved paths
                        _LOGGER.debug("SSL enabled, creating SSL context in executor...")
                        ssl_context = await self.hass.async_add_executor_job(
                            _create_ssl_context, 
                            root_cert, 
                            cert_file, 
                            key_file
                        )
                        connect_args = {"ssl": ssl_context}
                    else:
                        # Disable SSL explicitly to prevent asyncpg from auto-detecting
                        # certificates and doing blocking I/O
                        _LOGGER.debug("SSL disabled (default)")
                        connect_args = {"ssl": False}
                    
                    # Create engine
                    self._engine = create_async_engine(
                        self.db_url,
                        pool_size=10,
                        max_overflow=20,
                        echo=False,
                        connect_args=connect_args
                    )
                    
                    _LOGGER.debug("AsyncEngine created successfully")
                except Exception as e:
                    _LOGGER.error(f"Failed to create engine: {e}", exc_info=True)
                    return

            # Initialize DB
            _LOGGER.debug("Starting database initialization...")
            try:
                await self.init_db()
            except Exception as e:
                 _LOGGER.error(f"Failed to initialize database: {e}", exc_info=True)

            _LOGGER.debug("Database initialization completed")
            
            # Fetch initial counts
            try:
                await self._get_initial_counts()
            except Exception as e:
                 _LOGGER.warning(f"Failed to fetch initial counts: {e}")
            
            # Start Loop
            self._task = asyncio.create_task(self._run())
            _LOGGER.info("ScribeWriter started successfully")

        except Exception as e:
             _LOGGER.error(f"Unexpected error starting ScribeWriter: {e}", exc_info=True)

    async def _get_initial_counts(self):
        """Fetch initial row counts from database."""
        _LOGGER.debug("Fetching initial row counts...")
        try:
            async with self._engine.connect() as conn:
                if self.record_states:
                    res = await conn.execute(text(f"SELECT count(*) FROM {self.table_name_states}"))
                    self._states_written = res.scalar() or 0
                
                if self.record_events:
                    res = await conn.execute(text(f"SELECT count(*) FROM {self.table_name_events}"))
                    self._events_written = res.scalar() or 0
                    
            _LOGGER.debug(f"Initial counts: states={self._states_written}, events={self._events_written}")
        except Exception as e:
            _LOGGER.warning(f"Failed to fetch initial counts: {e}")



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
            except Exception as e:
                _LOGGER.error(f"Error waiting for writer task to stop: {e}", exc_info=True)
        
        # Final flush
        try:
            await self._flush()
        except Exception as e:
            _LOGGER.error(f"Error during final flush: {e}", exc_info=True)
        
        if self._engine:
            try:
                await self._engine.dispose()
                _LOGGER.debug("Engine disposed")
            except Exception as e:
                 _LOGGER.error(f"Error disposing engine: {e}", exc_info=True)

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
                _LOGGER.error(f"Error in writer loop: {e}", exc_info=True)
                # Prevent tight loop if persistent error
                await asyncio.sleep(5)

    def enqueue(self, data: Dict[str, Any]):
        """Add data to the queue.
        
        This is called from the main loop, so it shouldn't block.
        We use deque with maxlen, so old items are automatically dropped if full.
        """
        try:
            self._queue.append(data)
            
            # Trigger flush if batch size reached (but only if no flush is already pending)
            if len(self._queue) >= self.batch_size and not self._flush_pending:
                self._flush_pending = True
                _LOGGER.debug(f"Batch size reached ({len(self._queue)} >= {self.batch_size}), triggering flush")
                asyncio.create_task(self._flush())
        except Exception as e:
            _LOGGER.error(f"Error enqueuing data: {e}", exc_info=True)

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
                
                # Always init users table
                if self.enable_table_users:
                    await self._init_users_table(conn)
                if self.enable_table_entities:
                    await self._init_entities_table(conn)
                if self.enable_table_areas:
                    await self._init_areas_table(conn)
                if self.enable_table_devices:
                    await self._init_devices_table(conn)
                if self.enable_table_integrations:
                    await self._init_integrations_table(conn)

            # Hypertable & Compression (each operation in its own transaction)
            if self.record_states:
                try:
                     await self._init_hypertable(self.table_name_states, "entity_id")
                except Exception as e:
                     _LOGGER.error(f"Failed to init hypertable/compression for states: {e}", exc_info=True)
            
            if self.record_events:
                try:
                    await self._init_hypertable(self.table_name_events, "event_type")
                except Exception as e:
                     _LOGGER.error(f"Failed to init hypertable/compression for events: {e}", exc_info=True)
                    
            _LOGGER.info("Database initialized successfully")
            self._connected = True

        except Exception as e:
            _LOGGER.error(f"Error initializing database: {e}", exc_info=True)
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

    async def _init_users_table(self, conn):
        """Initialize users table."""
        _LOGGER.debug("Creating table users if not exists")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                is_owner BOOLEAN,
                is_active BOOLEAN,
                system_generated BOOLEAN,
                group_ids JSONB
            );
        """))

    async def write_users(self, users: list[dict]):
        """Write users to the database (upsert)."""
        if not self._engine or not users:
            return

        _LOGGER.debug(f"Writing {len(users)} users to database...")
        try:
            async with self._engine.begin() as conn:
                # Upsert users
                # We use ON CONFLICT DO UPDATE to update existing users
                stmt = text("""
                    INSERT INTO users (user_id, name, is_owner, is_active, system_generated, group_ids)
                    VALUES (:user_id, :name, :is_owner, :is_active, :system_generated, :group_ids)
                    ON CONFLICT (user_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        is_owner = EXCLUDED.is_owner,
                        is_active = EXCLUDED.is_active,
                        system_generated = EXCLUDED.system_generated,
                        group_ids = EXCLUDED.group_ids;
                """)
                await conn.execute(stmt, users)
                _LOGGER.debug("Users written successfully")
        except Exception as e:
            _LOGGER.error(f"Error writing users: {e}", exc_info=True)

    async def _init_entities_table(self, conn):
        """Initialize entities table."""
        _LOGGER.debug("Creating table entities if not exists")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                unique_id TEXT,
                platform TEXT,
                domain TEXT,
                name TEXT,
                device_id TEXT,
                area_id TEXT,
                capabilities JSONB
            );
        """))

    async def write_entities(self, entities: list[dict]):
        """Write entities to the database (upsert)."""
        if not self._engine or not entities:
            return

        _LOGGER.debug(f"Writing {len(entities)} entities to database...")
        try:
            async with self._engine.begin() as conn:
                # Upsert entities
                stmt = text("""
                    INSERT INTO entities (entity_id, unique_id, platform, domain, name, device_id, area_id, capabilities)
                    VALUES (:entity_id, :unique_id, :platform, :domain, :name, :device_id, :area_id, :capabilities)
                    ON CONFLICT (entity_id) DO UPDATE SET
                        unique_id = EXCLUDED.unique_id,
                        platform = EXCLUDED.platform,
                        domain = EXCLUDED.domain,
                        name = EXCLUDED.name,
                        device_id = EXCLUDED.device_id,
                        area_id = EXCLUDED.area_id,
                        capabilities = EXCLUDED.capabilities;
                """)
                await conn.execute(stmt, entities)
                _LOGGER.debug("Entities written successfully")
        except Exception as e:
            _LOGGER.error(f"Error writing entities: {e}", exc_info=True)

    async def _init_areas_table(self, conn):
        """Initialize areas table."""
        _LOGGER.debug("Creating table areas if not exists")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS areas (
                area_id TEXT PRIMARY KEY,
                name TEXT,
                picture TEXT
            );
        """))

    async def write_areas(self, areas: list[dict]):
        """Write areas to the database (upsert)."""
        if not self._engine or not areas:
            return

        _LOGGER.debug(f"Writing {len(areas)} areas to database...")
        try:
            async with self._engine.begin() as conn:
                stmt = text("""
                    INSERT INTO areas (area_id, name, picture)
                    VALUES (:area_id, :name, :picture)
                    ON CONFLICT (area_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        picture = EXCLUDED.picture;
                """)
                await conn.execute(stmt, areas)
                _LOGGER.debug("Areas written successfully")
        except Exception as e:
            _LOGGER.error(f"Error writing areas: {e}", exc_info=True)

    async def _init_devices_table(self, conn):
        """Initialize devices table."""
        _LOGGER.debug("Creating table devices if not exists")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT,
                name_by_user TEXT,
                model TEXT,
                manufacturer TEXT,
                sw_version TEXT,
                area_id TEXT,
                primary_config_entry TEXT
            );
        """))

    async def write_devices(self, devices: list[dict]):
        """Write devices to the database (upsert)."""
        if not self._engine or not devices:
            return

        _LOGGER.debug(f"Writing {len(devices)} devices to database...")
        
        try:
            # Sanitize text fields (ensure string)
            text_fields = ["sw_version", "model", "manufacturer", "name", "name_by_user"]
            for device in devices:
                for field in text_fields:
                   if device.get(field) is not None:
                       device[field] = str(device[field])

            async with self._engine.begin() as conn:
                stmt = text("""
                    INSERT INTO devices (device_id, name, name_by_user, model, manufacturer, sw_version, area_id, primary_config_entry)
                    VALUES (:device_id, :name, :name_by_user, :model, :manufacturer, :sw_version, :area_id, :primary_config_entry)
                    ON CONFLICT (device_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        name_by_user = EXCLUDED.name_by_user,
                        model = EXCLUDED.model,
                        manufacturer = EXCLUDED.manufacturer,
                        sw_version = EXCLUDED.sw_version,
                        area_id = EXCLUDED.area_id,
                        primary_config_entry = EXCLUDED.primary_config_entry;
                """)
                await conn.execute(stmt, devices)
                _LOGGER.debug("Devices written successfully")
        except Exception as e:
            _LOGGER.error(f"Error writing devices: {e}", exc_info=True)

    async def _init_integrations_table(self, conn):
        """Initialize integrations table."""
        _LOGGER.debug("Creating table integrations if not exists")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS integrations (
                entry_id TEXT PRIMARY KEY,
                domain TEXT,
                title TEXT,
                state TEXT,
                source TEXT
            );
        """))

    async def write_integrations(self, integrations: list[dict]):
        """Write integrations to the database (upsert)."""
        if not self._engine or not integrations:
            return

        _LOGGER.debug(f"Writing {len(integrations)} integrations to database...")
        try:
            async with self._engine.begin() as conn:
                stmt = text("""
                    INSERT INTO integrations (entry_id, domain, title, state, source)
                    VALUES (:entry_id, :domain, :title, :state, :source)
                    ON CONFLICT (entry_id) DO UPDATE SET
                        domain = EXCLUDED.domain,
                        title = EXCLUDED.title,
                        state = EXCLUDED.state,
                        source = EXCLUDED.source;
                """)
                await conn.execute(stmt, integrations)
                _LOGGER.debug("Integrations written successfully")
        except Exception as e:
            _LOGGER.error(f"Error writing integrations: {e}", exc_info=True)

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
            _LOGGER.warning(f"Hypertable creation failed (might not be TimescaleDB or already exists): {e}")

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

    def _sanitize_obj(self, obj: Any, depth: int = 0) -> Any:
        try:
            if depth > 100:
                return str(obj)

            if isinstance(obj, str):
                if "\0" in obj:
                    _LOGGER.warning(f"Sanitized string containing null byte: {obj!r}")
                    return obj.replace("\0", "")
                return obj
            if isinstance(obj, dict):
                 return {k: self._sanitize_obj(v, depth + 1) for k, v in obj.items()}
            if isinstance(obj, list):
                 return [self._sanitize_obj(v, depth + 1) for v in obj]
            if isinstance(obj, tuple):
                 return tuple(self._sanitize_obj(v, depth + 1) for v in obj)
            return obj
        except Exception as e:
            _LOGGER.error(f"Error serializing obj: {e}", exc_info=True)
            return obj

    async def _flush(self):
        """Flush the queue to the database."""
        try:
            self._flush_pending = False  # Reset flag immediately
            
            # Prune history first (maintain rolling window even if idle)
            now = time.time()
            while self._states_history and now - self._states_history[0][0] > 60:
                self._states_history.popleft()
            while self._events_history and now - self._events_history[0][0] > 60:
                self._events_history.popleft()

            if not self._queue:
                return

            # Swap queue - drain the deque
            batch = list(self._queue)
            self._queue.clear()
            
            start_time = time.time()
            
            try:
                def _process_batch(batch_items):
                    sanitized_batch = [self._sanitize_obj(item) for item in batch_items]
                    
                    states_res = []
                    events_res = []
                    
                    for x in sanitized_batch:
                        if x['type'] == 'state':
                            if isinstance(x.get('attributes'), dict):
                                x['attributes'] = json.dumps(x['attributes'], default=str)
                            states_res.append(x)
                        elif x['type'] == 'event':
                            if isinstance(x.get('event_data'), dict):
                                x['event_data'] = json.dumps(x['event_data'], cls=JSONEncoder)
                            events_res.append(x)
                    return states_res, events_res

                # Run CPU-intensive serialization in executor
                states_data, events_data = await self.hass.async_add_executor_job(_process_batch, batch)

                async with self._engine.begin() as conn:
                    if states_data:
                        await conn.execute(
                            text(f"INSERT INTO {self.table_name_states} (time, entity_id, state, value, attributes) VALUES (:time, :entity_id, :state, :value, :attributes)"),
                            states_data
                        )
                    if events_data:
                        await conn.execute(
                            text(f"INSERT INTO {self.table_name_events} (time, event_type, event_data, origin, context_id, context_user_id, context_parent_id) VALUES (:time, :event_type, :event_data, :origin, :context_id, :context_user_id, :context_parent_id)"),
                            events_data
                        )
                
                duration = time.time() - start_time
                self._states_written += len(states_data)
                self._events_written += len(events_data)
                
                # Update history for rates
                self._states_history.append((time.time(), len(states_data)))
                self._events_history.append((time.time(), len(events_data)))
                
                self._last_write_duration = duration
                
                if not self._connected:
                     _LOGGER.info(f"Database connection restored. Flushed {len(states_data)} states and {len(events_data)} events.")

                self._connected = True
                self._last_error = None

            except SQLAlchemyError as e:
                # simplify log message for DB errors
                msg = str(e.orig) if hasattr(e, "orig") else str(e)
                # If msg is still too long/ugly, just truncating or picking the first line
                if "\n" in msg:
                    msg = msg.split("\n")[0]
                
                _LOGGER.error(f"Database error during flush: {msg}")

                self._connected = False
                self._last_error = msg
                
                if self.buffer_on_failure:
                    _LOGGER.warning(f"Buffering {len(batch)} items due to failure. Current queue size: {len(self._queue)}")
                    
            except Exception as e:
                _LOGGER.error(f"Error flushing batch: {e}")
                self._connected = False
                self._last_error = str(e)
                
                if self.buffer_on_failure:
                    _LOGGER.warning(f"Buffering {len(batch)} items due to failure. Current queue size: {len(self._queue)}")
                    # Prepend back to queue
                    # We want to keep [OldBatch] + [NewQueue], but capped at max_queue_size.
                    # Since we want to drop OLDEST items if full, we want the TAIL of this combined list.
                    # deque(..., maxlen=N) keeps the tail (newest items).
                    # So we reconstruct the deque with the combined list.
                    self._queue = deque(batch + list(self._queue), maxlen=self.max_queue_size)
                    
                    # Calculate dropped
                    # current_len = len(self._queue)
                    # total_len = len(batch) + len(self._queue) - len(batch) # wait, this is just len(batch) + old_len
                    # Actually, we can't easily know how many were dropped by deque without checking lengths before/after
                    # But we know we added len(batch).
                    # If we were full, we dropped some.
                    
                    # Let's just log the current state
                    if len(self._queue) == self.max_queue_size:
                         _LOGGER.warning(f"Buffer full! Queue size: {len(self._queue)}")
                else:
                    self._dropped_events += len(batch)
                    _LOGGER.warning(f"Dropped {len(batch)} items (buffering disabled)")
        except Exception as e:
            _LOGGER.error(f"Critical error in _flush: {e}", exc_info=True)


    async def query(self, sql: str) -> list[dict]:
        """Execute a read-only SQL query."""
        if not self._engine:
            raise RuntimeError("Database not connected")

        # Security: Enforce Read-Only Transaction
        _LOGGER.debug(f"Executing query (Read-Only): {sql}")
        try:
            async with self._engine.connect() as conn:
                # set transaction to read only
                await conn.begin()
                await conn.execute(text("SET LOCAL TRANSACTION READ ONLY"))
                
                try:
                    result = await conn.execute(text(sql))
                    # Convert rows to list of dicts
                    return [dict(row._mapping) for row in result]
                finally:
                    await conn.rollback()
        except Exception as e:
            _LOGGER.error(f"Error executing query: {e}")
            raise e

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
        
        # Prepare tasks for concurrent execution
        tasks = []
        
        # Helper functions for queries
        async def get_states_chunk_stats():
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
                        return {
                            "states_total_chunks": row[0] or 0,
                            "states_compressed_chunks": row[1] or 0,
                            "states_uncompressed_chunks": row[2] or 0
                        }
            except Exception as e:
                _LOGGER.debug(f"Failed to get states chunk stats: {e}")
            return {}

        async def get_states_size_stats():
            total_bytes = 0
            compressed_bytes = 0

            try:
                # 1. Get Total Size (Compressed + Uncompressed)
                async with self._engine.connect() as conn:
                    res_total = await conn.execute(text(f"SELECT total_bytes FROM hypertable_detailed_size('{self.table_name_states}')"))
                    row_total = res_total.fetchone()
                    total_bytes = row_total[0] if row_total else 0
            except Exception as e:
                _LOGGER.debug(f"Failed to get states total size: {e}")
            
            try:
                # 2. Get Compressed Size
                async with self._engine.connect() as conn:
                    res_comp = await conn.execute(text(f"SELECT after_compression_total_bytes FROM hypertable_compression_stats('{self.table_name_states}')"))
                    row_comp = res_comp.fetchone()
                    compressed_bytes = row_comp[0] if row_comp else 0
            except Exception as e:
                _LOGGER.debug(f"Failed to get states compressed size: {e}")

            return {
                "states_total_size": total_bytes,
                "states_compressed_size": compressed_bytes,
                "states_uncompressed_size": max(0, total_bytes - compressed_bytes)
            }

        async def get_events_chunk_stats():
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
                        return {
                            "events_total_chunks": row[0] or 0,
                            "events_compressed_chunks": row[1] or 0,
                            "events_uncompressed_chunks": row[2] or 0
                        }
            except Exception as e:
                _LOGGER.debug(f"Failed to get events chunk stats: {e}")
            return {}

        async def get_events_size_stats():
            total_bytes = 0
            compressed_bytes = 0

            try:
                # 1. Get Total Size (Compressed + Uncompressed)
                async with self._engine.connect() as conn:
                    res_total = await conn.execute(text(f"SELECT total_bytes FROM hypertable_detailed_size('{self.table_name_events}')"))
                    row_total = res_total.fetchone()
                    total_bytes = row_total[0] if row_total else 0
            except Exception as e:
                _LOGGER.debug(f"Failed to get events total size: {e}")
            
            try:
                # 2. Get Compressed Size
                async with self._engine.connect() as conn:
                    res_comp = await conn.execute(text(f"SELECT after_compression_total_bytes FROM hypertable_compression_stats('{self.table_name_events}')"))
                    row_comp = res_comp.fetchone()
                    compressed_bytes = row_comp[0] if row_comp else 0
            except Exception as e:
                _LOGGER.debug(f"Failed to get events compressed size: {e}")

            return {
                "events_total_size": total_bytes,
                "events_compressed_size": compressed_bytes,
                "events_uncompressed_size": max(0, total_bytes - compressed_bytes)
            }

        async def get_states_compression_stats():
            try:
                async with self._engine.connect() as conn:
                    res = await conn.execute(text(f"SELECT * FROM hypertable_compression_stats('{self.table_name_states}')"))
                    row = res.fetchone()
                    if row:
                        return {
                            "states_before_compression_total_bytes": row.before_compression_total_bytes or 0,
                            "states_after_compression_total_bytes": row.after_compression_total_bytes or 0
                        }
            except Exception as e:
                # This might fail if compression is not enabled or table doesn't exist
                _LOGGER.debug(f"Failed to get states compression stats: {e}")
            return {}

        async def get_events_compression_stats():
            try:
                async with self._engine.connect() as conn:
                    res = await conn.execute(text(f"SELECT * FROM hypertable_compression_stats('{self.table_name_events}')"))
                    row = res.fetchone()
                    if row:
                        return {
                            "events_before_compression_total_bytes": row.before_compression_total_bytes or 0,
                            "events_after_compression_total_bytes": row.after_compression_total_bytes or 0
                        }
            except Exception as e:
                _LOGGER.debug(f"Failed to get events compression stats: {e}")
            return {}

        # Add tasks based on configuration and requested stats_type
        if self.record_states:
            if stats_type in ("chunk", "all"):
                tasks.append(get_states_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(get_states_size_stats())
                tasks.append(get_states_compression_stats())

        if self.record_events:
            if stats_type in ("chunk", "all"):
                tasks.append(get_events_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(get_events_size_stats())
                tasks.append(get_events_compression_stats())

        # Execute all tasks concurrently
        if tasks:
            results = await asyncio.gather(*tasks)
            for result in results:
                stats.update(result)
            
        return stats

    @property
    def running(self):
        return self._running

    @property
    def buffer_size(self):
        return len(self._queue)

    @property
    def states_rate_minute(self):
        """Return states written per minute (rolling window)."""
        return sum(count for _, count in self._states_history)

    @property
    def events_rate_minute(self):
        """Return events written per minute (rolling window)."""
        return sum(count for _, count in self._events_history)