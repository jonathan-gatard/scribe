"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements an asyncio-based writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.

NOTE: This version uses asyncpg directly (no SQLAlchemy) to avoid the greenlet
dependency which is not available on Python 3.14 / Alpine Linux (Home Assistant OS).
"""

import logging
import asyncio
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any
from collections import deque
import dataclasses
import json
import math
import uuid
from datetime import date, datetime as dt_datetime

import asyncpg

from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# NOTE: 'migration' is imported lazily (inside methods) to avoid circular imports.
# __init__.py imports ScribeWriter, so a top-level 'from . import migration' here
# would trigger a circular import before the package is fully initialized.

_LOGGER = logging.getLogger(__name__)


def _create_ssl_context(
    ssl_root_cert=None, ssl_cert_file=None, ssl_key_file=None
) -> ssl.SSLContext:
    """Create and configure SSL context in executor thread.

    asyncpg calls ssl.load_cert_chain() synchronously when establishing SSL connections.
    By creating the SSLContext here (in an executor thread) and passing it to asyncpg,
    we avoid blocking Home Assistant's event loop.

    This function must be run via hass.async_add_executor_job().

    Returns:
        Configured SSLContext ready to be used by asyncpg
    """
    _LOGGER.debug(
        "[writer._create_ssl_context] Creating SSL context in executor thread..."
    )

    # Create SSL context
    ssl_context = ssl.create_default_context()

    # Load system CA certificates
    try:
        ssl_context.load_default_certs()
        _LOGGER.debug("[writer._create_ssl_context] Loaded system CA certificates")
    except Exception as e:
        _LOGGER.warning(
            "[writer._create_ssl_context] Could not load system CA certificates: %s (%s) — continuing with built-in defaults",
            e,
            type(e).__name__,
        )

    # Load PostgreSQL client certificates
    if ssl_cert_file:
        if Path(ssl_cert_file).exists():
            try:
                _LOGGER.debug(
                    "[writer._create_ssl_context] Loading PostgreSQL client certificate from %s (key=%s)",
                    ssl_cert_file,
                    ssl_key_file,
                )
                ssl_context.load_cert_chain(ssl_cert_file, ssl_key_file)
            except Exception as e:
                _LOGGER.error(
                    "[writer._create_ssl_context] Could not load cert chain from %s (key=%s): %s (%s)",
                    ssl_cert_file,
                    ssl_key_file,
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
        else:
            _LOGGER.warning(
                "[writer._create_ssl_context] SSL cert file configured but not found: %s — connection will proceed without client certificate",
                ssl_cert_file,
            )

    # Load CA certificate for server verification
    if ssl_root_cert:
        if Path(ssl_root_cert).exists():
            try:
                _LOGGER.debug(
                    "[writer._create_ssl_context] Loading CA certificate from %s",
                    ssl_root_cert,
                )
                ssl_context.load_verify_locations(ssl_root_cert)
            except Exception as e:
                _LOGGER.error(
                    "[writer._create_ssl_context] Could not load CA cert from %s: %s (%s)",
                    ssl_root_cert,
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
        else:
            _LOGGER.warning(
                "[writer._create_ssl_context] SSL root cert configured but not found: %s — server certificate will not be verified",
                ssl_root_cert,
            )

    _LOGGER.debug("[writer._create_ssl_context] SSL context created successfully")
    return ssl_context


def _normalize_dsn(db_url: str) -> str:
    """Convert SQLAlchemy-style DSN to plain asyncpg DSN.

    asyncpg uses postgresql:// (or postgres://), not postgresql+asyncpg://.
    """
    return db_url.replace("postgresql+asyncpg://", "postgresql://")


def _safe_target(db_url: str) -> str:
    """Describe where a DSN points, with no chance of carrying its credentials.

    This ends up in logs and in Repairs cards, so it is built from the parsed
    host, port and database name rather than by slicing the URL — a split on
    '@' happens to be safe today but cannot be proven so, and one edit away
    from leaking a password.
    """
    try:
        parts = urlsplit(db_url)
        target = parts.hostname or "?"
        if parts.port:
            target = f"{target}:{parts.port}"
        database = parts.path.lstrip("/")
        return f"{target}/{database}" if database else target
    except Exception:
        return "?"


ISSUE_LEARN_MORE_URL = "https://github.com/jonathan-gtd/scribe#troubleshooting"

# Repairs issue ids. Fixed strings (not per-entity) so a recurring condition
# updates one entry and resolving it deletes that same entry.
ISSUE_DB_UNREACHABLE = "db_unreachable"
ISSUE_WRITE_FAILING = "write_failing"
ISSUE_BUFFER_FULL = "buffer_full"
ISSUE_DATA_DROPPED = "data_dropped"
ISSUE_NO_TIMESCALEDB = "no_timescaledb"
ISSUE_MIGRATION_FAILED = "migration_failed"

# How many consecutive failed flushes before bothering the user: a single
# failure is a blip (a restart, a brief network drop) and self-heals.
WRITE_FAILURE_ISSUE_THRESHOLD = 3


def _json_default(obj):
    """Encode values json cannot serialize natively, for the jsonb codec.

    `_sanitize_obj` already stringifies unknown objects, so little reaches
    here — but a bare ``date`` does, and Home Assistant's JSONEncoder only
    handles ``datetime``. Without this it raises TypeError and takes the whole
    flush batch down with it.
    """
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (dt_datetime, date)):
        return obj.isoformat()
    return JSONEncoder().default(obj)


def _affected_rows(status: str) -> int:
    """Row count from an asyncpg command tag ('UPDATE 12'), or -1 if unparsable."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return -1


def _validate_table_name(name: str) -> str:
    """Validate that a table name contains only safe characters.

    Table names are used in SQL f-strings and must be restricted to
    alphanumeric characters and underscores to prevent SQL injection.

    Raises ValueError if the name is invalid.
    """
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(
            f"Invalid table name '{name}': only letters, digits, and underscores are allowed"
        )
    return name


class ScribeWriter:
    """Handle database connections and writing.

    This class runs as an asyncio task. It maintains a queue of events to be written.
    Data is flushed to the database when the queue reaches BATCH_SIZE or when
    FLUSH_INTERVAL seconds have passed.

    Uses asyncpg directly (no SQLAlchemy) to avoid the greenlet dependency.
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
        ssl_root_cert: str | None = None,
        ssl_cert_file: str | None = None,
        ssl_key_file: str | None = None,
        enable_table_areas: bool = True,
        enable_table_devices: bool = True,
        enable_table_integrations: bool = True,
        enable_table_users: bool = True,
    ):
        """Initialize the writer."""
        self.hass = hass

        # Normalize DSN - strip SQLAlchemy dialect prefix if present
        self.db_url = _normalize_dsn(db_url)

        self.chunk_interval = chunk_interval
        self.compress_after = compress_after
        self.record_states = record_states
        self.record_events = record_events
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_queue_size = max_queue_size
        self.buffer_on_failure = buffer_on_failure
        self.table_name_states = _validate_table_name(table_name_states)
        self.table_name_events = _validate_table_name(table_name_events)
        self.use_ssl = use_ssl
        self.ssl_root_cert = ssl_root_cert
        self.ssl_cert_file = ssl_cert_file
        self.ssl_key_file = ssl_key_file
        self.enable_table_areas = enable_table_areas
        self.enable_table_devices = enable_table_devices
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
        self._flush_pending = False  # Prevent multiple flush tasks
        # Serializes every write that touches entity metadata (rename_entity,
        # write_entities, and the flush section that resolves/uses metadata_ids).
        # HA fires registry events as concurrent tasks: without this, a metadata
        # sync can insert the destination row mid-rename (self-collision), or a
        # flush can COPY states to a metadata_id a rename just merged away.
        self._metadata_lock = asyncio.Lock()
        # Consecutive failed flushes, used to decide when a transient blip has
        # become a condition worth surfacing in Repairs.
        self._consecutive_flush_failures = 0

        # asyncpg connection pool (replaces SQLAlchemy engine)
        self._pool: asyncpg.Pool = None
        # Keep _engine as alias for migration.py compatibility
        self._engine = None

        self._task = None
        self._running = False

        # ID Cache: entity_id -> metadata_id
        self._entity_id_map: dict[str, int] = {}
        # Reverse Cache: metadata_id -> entity_id (for debugging/renames if needed)
        self._metadata_id_map: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Internal helpers: acquire connection with/without transaction
    # ------------------------------------------------------------------

    async def _execute(self, sql: str, *args):
        """Execute a statement (no return value needed) using a pooled connection."""
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *args)

    async def _execute_many(self, sql: str, args_list: list):
        """Execute a statement for each row in args_list inside a transaction."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(sql, args_list)

    async def _copy_records(
        self,
        conn: asyncpg.Connection,
        table_name: str,
        columns: list[str],
        records: list[tuple[Any, ...]],
        conflict_target: str | None = None,
    ):
        """Write batched records via PostgreSQL COPY, falling back to executemany if unavailable.

        COPY has no ON CONFLICT clause, so a row colliding with one already in
        the table aborts the batch. When `conflict_target` is given, such a
        collision is retried row-by-row with ON CONFLICT DO NOTHING: slower,
        but only on the failure path, and it keeps a re-buffered batch that
        overlaps already-written history from failing forever.
        """
        if not records:
            return

        if hasattr(conn, "copy_records_to_table"):
            try:
                # SAVEPOINT: Postgres marks a transaction failed after any
                # error, so the fallback below needs the error contained.
                async with conn.transaction():
                    await conn.copy_records_to_table(
                        table_name=table_name,
                        records=records,
                        columns=columns,
                    )
                return
            except asyncpg.UniqueViolationError:
                if conflict_target is None:
                    raise
                _LOGGER.warning(
                    "[writer._copy_records] COPY into %s hit an existing row; "
                    "retrying %d records with ON CONFLICT (%s) DO NOTHING.",
                    table_name,
                    len(records),
                    conflict_target,
                )

        placeholders = ", ".join(f"${idx}" for idx in range(1, len(columns) + 1))
        fallback_sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        if conflict_target:
            fallback_sql += f" ON CONFLICT ({conflict_target}) DO NOTHING"
        await conn.executemany(fallback_sql, records)

    async def _fetchval(self, sql: str, *args):
        """Fetch a single scalar value."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def _fetchrow(self, sql: str, *args):
        """Fetch a single row."""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _fetch(self, sql: str, *args):
        """Fetch all rows."""
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the writer task."""
        try:
            if self._running:
                return

            _LOGGER.debug("[writer.start] Starting ScribeWriter...")
            self._running = True

            # Create connection pool
            if not self._pool:
                try:
                    _LOGGER.debug(
                        "[writer.start] Creating asyncpg pool for %s",
                        _safe_target(self.db_url),
                    )

                    ssl_arg = False  # default: no SSL
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
                        _LOGGER.debug(
                            "[writer.start] SSL enabled, creating SSL context in executor..."
                        )
                        ssl_arg = await self.hass.async_add_executor_job(
                            _create_ssl_context, root_cert, cert_file, key_file
                        )

                    async def _init_connection(conn):
                        await conn.set_type_codec(
                            "jsonb",
                            encoder=lambda x: (
                                b"\x01"
                                + json.dumps(
                                    x, cls=JSONEncoder, default=_json_default
                                ).encode("utf-8")
                            ),
                            decoder=lambda x: json.loads(x[1:].decode("utf-8")),
                            schema="pg_catalog",
                            format="binary",
                        )

                    self._pool = await asyncpg.create_pool(
                        dsn=self.db_url,
                        min_size=1,
                        max_size=10,
                        ssl=ssl_arg,
                        init=_init_connection,
                    )
                    # Expose pool as _engine so migration.py can use it
                    self._engine = self._pool

                    _LOGGER.debug(
                        "[writer.start] asyncpg pool created successfully (host=%s, ssl=%s)",
                        _safe_target(self.db_url),
                        bool(ssl_arg),
                    )
                    self._clear_issue(ISSUE_DB_UNREACHABLE)
                except Exception as e:
                    _LOGGER.error(
                        "[writer.start] Failed to create asyncpg pool for %s: %s (%s). Check DB URL, credentials, network and SSL configuration.",
                        _safe_target(self.db_url),
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )
                    # Nothing will be recorded at all until this is fixed, and
                    # the only other signal is a log line at startup.
                    self._report_issue(
                        ISSUE_DB_UNREACHABLE,
                        "db_unreachable",
                        {
                            "host": _safe_target(self.db_url),
                            "error": f"{e} ({type(e).__name__})",
                        },
                        severity=ir.IssueSeverity.ERROR,
                    )
                    self._running = False
                    return

            # Perform initialization
            try:
                _LOGGER.debug("[writer.start] Starting database initialization...")
                await self.init_db()
                _LOGGER.debug("[writer.start] Database initialization completed")

                # Loop launch (background)
                self._task = asyncio.create_task(self._run())
                _LOGGER.info("[writer.start] ScribeWriter started successfully")
            except Exception as e:
                _LOGGER.error(
                    "[writer.start] Database initialization failed: %s (%s)",
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                self._connected = False
                raise

        except Exception as e:
            _LOGGER.error(
                "[writer.start] Unexpected error starting ScribeWriter: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )
            raise

    async def _get_initial_counts(self):
        """Fetch initial row counts from database."""
        _LOGGER.debug("[writer._get_initial_counts] Fetching initial row counts...")
        try:
            if self.record_states:
                self._states_written = (
                    await self._fetchval(
                        f"SELECT count(*) FROM {self.table_name_states}"
                    )
                    or 0
                )

            if self.record_events:
                self._events_written = (
                    await self._fetchval(
                        f"SELECT count(*) FROM {self.table_name_events}"
                    )
                    or 0
                )

            _LOGGER.debug(
                "[writer._get_initial_counts] Initial counts: states=%d, events=%d",
                self._states_written,
                self._events_written,
            )
        except Exception as e:
            _LOGGER.warning(
                "[writer._get_initial_counts] Failed to fetch initial counts from tables (states=%s, events=%s): %s (%s)",
                self.table_name_states,
                self.table_name_events,
                e,
                type(e).__name__,
            )

    async def _ensure_metadata_ids(self, entity_ids: list[str]):
        """Ensure all entity_ids have a metadata_id in the cache."""
        missing = [eid for eid in entity_ids if eid not in self._entity_id_map]
        if not missing:
            return

        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Insert missing entities
                    await conn.executemany(
                        "INSERT INTO entities (entity_id) VALUES ($1) ON CONFLICT (entity_id) DO NOTHING",
                        [(eid,) for eid in missing],
                    )

                    # Fetch IDs for the missing ones
                    rows = await conn.fetch(
                        "SELECT entity_id, id FROM entities WHERE entity_id = ANY($1)",
                        missing,
                    )

                    count = 0
                    for row in rows:
                        self._entity_id_map[row["entity_id"]] = row["id"]
                        self._metadata_id_map[row["id"]] = row["entity_id"]
                        count += 1

                    if count > 0:
                        _LOGGER.debug(
                            "[writer._ensure_metadata_ids] Registered %d new entities (missing=%d)",
                            count,
                            len(missing),
                        )

        except Exception as e:
            _LOGGER.error(
                "[writer._ensure_metadata_ids] Error registering %d new entities (sample=%s): %s (%s)",
                len(missing),
                missing[:5],
                e,
                type(e).__name__,
                exc_info=True,
            )

    async def stop(self):
        """Stop the writer task."""
        _LOGGER.debug("[writer.stop] Stopping ScribeWriter...")
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                _LOGGER.debug("[writer.stop] Writer task cancelled as expected")
            except Exception as e:
                _LOGGER.error(
                    "[writer.stop] Error waiting for writer task to stop: %s (%s)",
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

        # Final flush
        try:
            await self._flush()
        except Exception as e:
            _LOGGER.error(
                "[writer.stop] Error during final flush (queue_size=%d): %s (%s)",
                len(self._queue),
                e,
                type(e).__name__,
                exc_info=True,
            )

        if self._pool:
            try:
                await self._pool.close()
                self._pool = None
                self._engine = None
                _LOGGER.debug("[writer.stop] Pool closed")
            except Exception as e:
                _LOGGER.error(
                    "[writer.stop] Error closing asyncpg pool: %s (%s)",
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

    async def _run(self):
        """Main loop."""
        _LOGGER.debug("[writer._run] ScribeWriter loop started")

        # 1. Register listener to launch migration AFTER HA finishes bootstrap
        async def _launch_migration(event):
            """Launch migration after HA is fully started."""
            _LOGGER.debug(
                "[writer._run._launch_migration] HA fully started, launching background migration task"
            )
            try:
                from . import migration  # lazy import to avoid circular dependency

                await migration.migrate_database(
                    self.hass,
                    self._pool,  # pass pool (migration.py uses it as 'engine')
                    self.record_states,
                    self.chunk_interval,
                    self.compress_after,
                )

                # Create the view now that migration is done (the table was renamed/dropped)
                async with self._pool.acquire() as conn:
                    await self._init_states_view(conn)

                self._clear_issue(ISSUE_MIGRATION_FAILED)

            except Exception as e:
                _LOGGER.error(
                    "[writer._run._launch_migration] Background migration failed: %s (%s)",
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                # A half-migrated database keeps the old history in
                # states_legacy where nothing reads it. Silence here means the
                # user believes their history was carried over when it was not.
                self._report_issue(
                    ISSUE_MIGRATION_FAILED,
                    "migration_failed",
                    {"error": f"{e} ({type(e).__name__})"},
                    severity=ir.IssueSeverity.ERROR,
                )

        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _launch_migration)

        # 2. Fetch initial counts (background - might take a while on large DBs)
        try:
            await self._get_initial_counts()
        except Exception as e:
            _LOGGER.warning(
                "[writer._run] Failed to fetch initial (background) counts: %s (%s)",
                e,
                type(e).__name__,
            )

        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(
                    "[writer._run] Error in writer loop (flush_interval=%ss, queue_size=%d): %s (%s)",
                    self.flush_interval,
                    len(self._queue),
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                # Prevent tight loop if persistent error
                await asyncio.sleep(5)

    def enqueue(self, data: dict[str, Any]):
        """Add data to the queue.

        This is called from the main loop, so it shouldn't block.
        We use deque with maxlen, so old items are automatically dropped if full.
        """
        try:
            if not self._running:
                return

            self._queue.append(data)

            # Trigger flush if batch size reached (but only if no flush is already pending)
            if len(self._queue) >= self.batch_size and not self._flush_pending:
                self._flush_pending = True
                _LOGGER.debug(
                    "[writer.enqueue] Batch size reached (%d >= %d), triggering flush",
                    len(self._queue),
                    self.batch_size,
                )
                asyncio.create_task(self._flush())
        except Exception as e:
            _LOGGER.error(
                "[writer.enqueue] Error enqueuing data (type=%s, keys=%s): %s (%s)",
                data.get("type") if isinstance(data, dict) else type(data).__name__,
                list(data.keys()) if isinstance(data, dict) else None,
                e,
                type(e).__name__,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------

    async def init_db(self):
        """Initialize database tables."""
        _LOGGER.debug("[writer.init_db] Initializing database...")
        if not self._pool:
            _LOGGER.warning(
                "[writer.init_db] No connection pool available, skipping DB initialization"
            )
            return

        try:
            # 1. Check and Perform Migration (own transaction)
            if self.record_states:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await self._check_and_migrate_states(conn)

            # 2. Create tables (own transaction)
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Initialize entities FIRST (states view depends on it,
                    # and runtime state writes always upsert into it)
                    await self._init_entities_table(conn)

                    # Always init users table
                    if self.enable_table_users:
                        await self._init_users_table(conn)

                    if self.enable_table_areas:
                        await self._init_areas_table(conn)
                    if self.enable_table_devices:
                        await self._init_devices_table(conn)
                    if self.enable_table_integrations:
                        await self._init_integrations_table(conn)

                    # Initialize states and events AFTER entities table exists
                    if self.record_states:
                        await self._init_states_table(conn)
                    if self.record_events:
                        await self._init_events_table(conn)

            # Hypertable & Compression (each operation in its own transaction)
            if self.record_states:
                try:
                    await self._init_hypertable("states_raw", "metadata_id")
                except Exception as e:
                    _LOGGER.error(
                        "[writer.init_db] Failed to init hypertable/compression for states_raw (chunk=%s, compress_after=%s): %s (%s)",
                        self.chunk_interval,
                        self.compress_after,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            if self.record_events:
                try:
                    await self._init_hypertable(self.table_name_events, "event_type")
                except Exception as e:
                    _LOGGER.error(
                        "[writer.init_db] Failed to init hypertable/compression for %s (chunk=%s, compress_after=%s): %s (%s)",
                        self.table_name_events,
                        self.chunk_interval,
                        self.compress_after,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

            await self._check_timescaledb_available()

            _LOGGER.info("[writer.init_db] Database initialized successfully")
            self._connected = True
            self._clear_issue(ISSUE_DB_UNREACHABLE)

        except Exception as e:
            _LOGGER.error(
                "[writer.init_db] Error initializing database: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )
            self._connected = False

    async def _check_and_migrate_states(self, conn):
        """Check if migration from 'states' (legacy) to 'states_raw' is needed."""
        try:
            states_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states' AND table_type = 'BASE TABLE')"
            )

            states_raw_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states_raw')"
            )

            if states_exists and not states_raw_exists:
                _LOGGER.warning(
                    "[writer._check_and_migrate_states] Detected legacy 'states' table. Starting migration to 'states_raw'..."
                )
                _LOGGER.info(
                    "[writer._check_and_migrate_states] 1/2 Renaming 'states' to 'states_legacy'. Data migration will happen in background."
                )
                await conn.execute("ALTER TABLE states RENAME TO states_legacy")

        except Exception as e:
            _LOGGER.error(
                "[writer._check_and_migrate_states] Migration check failed: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )

    async def _init_states_table(self, conn):
        """Initialize states_raw table and View."""

        # 1. Create states_raw
        _LOGGER.debug("Creating table states_raw if not exists")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS states_raw (
                time TIMESTAMPTZ NOT NULL,
                metadata_id INTEGER NOT NULL,
                state TEXT,
                value DOUBLE PRECISION,
                attributes JSONB,
                PRIMARY KEY (metadata_id, time)
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS states_raw_meta_time_idx 
            ON states_raw (metadata_id, time DESC);
        """)

        # 2. The view creation is now handled in `_init_states_view`
        # to ensure it doesn't conflict with the `states` table before migration.
        await self._init_states_view(conn)

    async def _init_states_view(self, conn):
        """Create the backward-compatible states view, if the name isn't taken by a table."""
        try:
            is_table = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = $1 AND table_type = 'BASE TABLE')",
                self.table_name_states,
            )
            if is_table:
                _LOGGER.debug(
                    "[writer._init_states_view] '%s' is currently a table. Skipping view creation until migration finishes.",
                    self.table_name_states,
                )
                return

            _LOGGER.debug(
                "[writer._init_states_view] Creating/Replacing view '%s'",
                self.table_name_states,
            )
            await conn.execute(f"DROP VIEW IF EXISTS {self.table_name_states} CASCADE;")
            await conn.execute(f"""
                CREATE VIEW {self.table_name_states} AS
                WITH drive AS MATERIALIZED (
                    SELECT * FROM entities
                )
                SELECT
                    s.time,
                    e.entity_id,
                    s.state,
                    s.value,
                    s.attributes
                FROM drive e
                CROSS JOIN LATERAL (
                    SELECT * FROM states_raw s
                    WHERE s.metadata_id = e.id
                ) s;
            """)
        except Exception as e:
            _LOGGER.error(
                "[writer._init_states_view] Failed to create view '%s' over 'states_raw': %s (%s)",
                self.table_name_states,
                e,
                type(e).__name__,
                exc_info=True,
            )

    async def _init_events_table(self, conn):
        """Initialize events table."""
        _LOGGER.debug(
            "[writer._init_events_table] Creating table %s if not exists",
            self.table_name_events,
        )
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name_events} (
                time TIMESTAMPTZ NOT NULL,
                event_type TEXT NOT NULL,
                event_data JSONB,
                origin TEXT,
                context_id TEXT,
                context_user_id TEXT,
                context_parent_id TEXT
            );
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {self.table_name_events}_type_time_idx 
            ON {self.table_name_events} (event_type, time DESC);
        """)

    async def _init_users_table(self, conn):
        """Initialize users table."""
        _LOGGER.debug("[writer._init_users_table] Creating table users if not exists")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                is_owner BOOLEAN,
                is_active BOOLEAN,
                system_generated BOOLEAN,
                group_ids JSONB
            );
        """)

    async def write_users(self, users: list[dict]):
        """Write users to the database (upsert)."""
        if not self._pool or not users:
            return

        _LOGGER.debug(
            "[writer.write_users] Writing %d users to database...", len(users)
        )
        try:
            # Sanitize text fields (ensure string, remove null bytes)
            text_fields = ["user_id", "name"]
            for user in users:
                for field in text_fields:
                    if user.get(field) is not None:
                        user[field] = str(user[field]).replace("\0", "")
                if user.get("group_ids"):
                    user["group_ids"] = self._sanitize_obj(user["group_ids"])

            rows = [
                (
                    u.get("user_id"),
                    u.get("name"),
                    u.get("is_owner"),
                    u.get("is_active"),
                    u.get("system_generated"),
                    u.get("group_ids"),
                )
                for u in users
            ]

            await self._execute_many(
                """
                INSERT INTO users (user_id, name, is_owner, is_active, system_generated, group_ids)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    is_owner = EXCLUDED.is_owner,
                    is_active = EXCLUDED.is_active,
                    system_generated = EXCLUDED.system_generated,
                    group_ids = EXCLUDED.group_ids;
            """,
                rows,
            )
            _LOGGER.debug("[writer.write_users] Users written successfully")
        except Exception as e:
            _LOGGER.error(
                "[writer.write_users] Error writing %d users: %s (%s)",
                len(users),
                e,
                type(e).__name__,
                exc_info=True,
            )

    async def _init_entities_table(self, conn):
        """Initialize entities table."""
        _LOGGER.debug(
            "[writer._init_entities_table] Creating table entities if not exists"
        )

        # Ensure schema is up to date (migrate from old text-PK schema if needed)
        from . import migration  # lazy import to avoid circular dependency

        await migration.migrate_entities_table(conn)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id SERIAL PRIMARY KEY,
                entity_id TEXT UNIQUE,
                unique_id TEXT,
                platform TEXT,
                domain TEXT,
                name TEXT,
                device_id TEXT,
                area_id TEXT,
                capabilities JSONB
            );
        """)

        # Index for entity lookup by entity_id (UNIQUE constraint already creates an index)
        # await conn.execute("CREATE INDEX IF NOT EXISTS entities_entity_id_idx ON entities (entity_id)")

        # Populate Cache on startup
        try:
            rows = await conn.fetch("SELECT entity_id, id FROM entities")
            for row in rows:
                self._entity_id_map[row["entity_id"]] = row["id"]
                self._metadata_id_map[row["id"]] = row["entity_id"]
            _LOGGER.debug(
                "[writer._init_entities_table] Loaded %d entities into ID cache",
                len(self._entity_id_map),
            )
        except Exception as e:
            _LOGGER.warning(
                "[writer._init_entities_table] Failed to populate entity cache: %s (%s)",
                e,
                type(e).__name__,
            )

    async def write_entities(self, entities: list[dict]):
        """Sync entities: INSERT new rows, UPDATE only changed rows, skip identical ones.

        Avoids `INSERT ... ON CONFLICT DO UPDATE`, which burns a SERIAL id on every
        conflicting row even when no insert happens — causing the id sequence to
        balloon on each full registry resync.
        """
        if not self._pool or not entities:
            return

        _LOGGER.debug(
            "[writer.write_entities] Processing %d entities...", len(entities)
        )
        # Serialized with rename_entity: without this, a registry-sync task can
        # insert the destination row while a rename of the same entity is
        # in flight (HA fires registry events as concurrent tasks).
        await self._metadata_lock.acquire()
        try:
            text_fields = [
                "entity_id",
                "unique_id",
                "platform",
                "domain",
                "name",
                "device_id",
                "area_id",
            ]
            for entity in entities:
                for field in text_fields:
                    if entity.get(field) is not None:
                        entity[field] = str(entity[field]).replace("\0", "")
                if entity.get("capabilities"):
                    entity["capabilities"] = self._sanitize_obj(entity["capabilities"])

            entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]
            if not entity_ids:
                return

            def _row_tuple(e: dict) -> tuple:
                return (
                    e.get("entity_id"),
                    e.get("unique_id"),
                    e.get("platform"),
                    e.get("domain"),
                    e.get("name"),
                    e.get("device_id"),
                    e.get("area_id"),
                    e.get("capabilities"),
                )

            def _unchanged(row, e: dict) -> bool:
                return (
                    row["unique_id"] == e.get("unique_id")
                    and row["platform"] == e.get("platform")
                    and row["domain"] == e.get("domain")
                    and row["name"] == e.get("name")
                    and row["device_id"] == e.get("device_id")
                    and row["area_id"] == e.get("area_id")
                    and row["capabilities"] == e.get("capabilities")
                )

            to_insert: list[tuple] = []
            to_update: list[tuple] = []

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    existing_rows = await conn.fetch(
                        """
                        SELECT id, entity_id, unique_id, platform, domain, name,
                               device_id, area_id, capabilities
                        FROM entities WHERE entity_id = ANY($1)
                        """,
                        entity_ids,
                    )
                    existing = {r["entity_id"]: r for r in existing_rows}

                    for e in entities:
                        eid = e.get("entity_id")
                        if not eid:
                            continue
                        row = existing.get(eid)
                        if row is None:
                            to_insert.append(_row_tuple(e))
                        elif not _unchanged(row, e):
                            to_update.append(_row_tuple(e))

                    if to_insert:
                        inserted_rows = await conn.fetch(
                            """
                            INSERT INTO entities (entity_id, unique_id, platform, domain, name, device_id, area_id, capabilities)
                            SELECT * FROM unnest(
                                $1::text[], $2::text[], $3::text[], $4::text[],
                                $5::text[], $6::text[], $7::text[], $8::jsonb[]
                            )
                            ON CONFLICT (entity_id) DO NOTHING
                            RETURNING id, entity_id
                            """,
                            [t[0] for t in to_insert],
                            [t[1] for t in to_insert],
                            [t[2] for t in to_insert],
                            [t[3] for t in to_insert],
                            [t[4] for t in to_insert],
                            [t[5] for t in to_insert],
                            [t[6] for t in to_insert],
                            [t[7] for t in to_insert],
                        )
                        for r in inserted_rows:
                            self._entity_id_map[r["entity_id"]] = r["id"]
                            self._metadata_id_map[r["id"]] = r["entity_id"]

                    if to_update:
                        await conn.executemany(
                            """
                            UPDATE entities SET
                                unique_id = $2,
                                platform = $3,
                                domain = $4,
                                name = $5,
                                device_id = $6,
                                area_id = $7,
                                capabilities = $8
                            WHERE entity_id = $1
                            """,
                            to_update,
                        )

            _LOGGER.debug(
                "[writer.write_entities] Done: %d inserted, %d updated, %d unchanged (of %d total)",
                len(to_insert),
                len(to_update),
                len(entities) - len(to_insert) - len(to_update),
                len(entities),
            )
        except Exception as e:
            _LOGGER.error(
                "[writer.write_entities] Error syncing %d entities (sample=%s): %s (%s)",
                len(entities),
                [e.get("entity_id") for e in entities[:3]],
                e,
                type(e).__name__,
                exc_info=True,
            )
        finally:
            self._metadata_lock.release()

    async def _init_areas_table(self, conn):
        """Initialize areas table."""
        _LOGGER.debug("[writer._init_areas_table] Creating table areas if not exists")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS areas (
                area_id TEXT PRIMARY KEY,
                name TEXT,
                picture TEXT
            );
        """)

    async def write_areas(self, areas: list[dict]):
        """Write areas to the database (upsert)."""
        if not self._pool or not areas:
            return

        _LOGGER.debug(
            "[writer.write_areas] Writing %d areas to database...", len(areas)
        )
        try:
            text_fields = ["area_id", "name", "picture"]
            for area in areas:
                for field in text_fields:
                    if area.get(field) is not None:
                        area[field] = str(area[field]).replace("\0", "")

            rows = [(a.get("area_id"), a.get("name"), a.get("picture")) for a in areas]

            await self._execute_many(
                """
                INSERT INTO areas (area_id, name, picture)
                VALUES ($1, $2, $3)
                ON CONFLICT (area_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    picture = EXCLUDED.picture;
            """,
                rows,
            )
            _LOGGER.debug("[writer.write_areas] Areas written successfully")
        except Exception as exc:
            _LOGGER.error(
                "[writer.write_areas] Error writing %d areas: %s (%s)",
                len(areas),
                exc,
                type(exc).__name__,
                exc_info=True,
            )

    async def _init_devices_table(self, conn):
        """Initialize devices table."""
        _LOGGER.debug(
            "[writer._init_devices_table] Creating table devices if not exists"
        )
        await conn.execute("""
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
        """)

    async def write_devices(self, devices: list[dict]):
        """Write devices to the database (upsert)."""
        if not self._pool or not devices:
            return

        _LOGGER.debug(
            "[writer.write_devices] Writing %d devices to database...", len(devices)
        )

        try:
            text_fields = [
                "device_id",
                "name",
                "name_by_user",
                "model",
                "manufacturer",
                "sw_version",
                "area_id",
                "primary_config_entry",
            ]
            for device in devices:
                for field in text_fields:
                    if device.get(field) is not None:
                        device[field] = str(device[field]).replace("\0", "")

            rows = [
                (
                    d.get("device_id"),
                    d.get("name"),
                    d.get("name_by_user"),
                    d.get("model"),
                    d.get("manufacturer"),
                    d.get("sw_version"),
                    d.get("area_id"),
                    d.get("primary_config_entry"),
                )
                for d in devices
            ]

            await self._execute_many(
                """
                INSERT INTO devices (device_id, name, name_by_user, model, manufacturer, sw_version, area_id, primary_config_entry)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    name_by_user = EXCLUDED.name_by_user,
                    model = EXCLUDED.model,
                    manufacturer = EXCLUDED.manufacturer,
                    sw_version = EXCLUDED.sw_version,
                    area_id = EXCLUDED.area_id,
                    primary_config_entry = EXCLUDED.primary_config_entry;
            """,
                rows,
            )
            _LOGGER.debug("[writer.write_devices] Devices written successfully")
        except Exception as exc:
            _LOGGER.error(
                "[writer.write_devices] Error writing %d devices: %s (%s)",
                len(devices),
                exc,
                type(exc).__name__,
                exc_info=True,
            )

    async def _init_integrations_table(self, conn):
        """Initialize integrations table."""
        _LOGGER.debug(
            "[writer._init_integrations_table] Creating table integrations if not exists"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS integrations (
                entry_id TEXT PRIMARY KEY,
                domain TEXT,
                title TEXT,
                state TEXT,
                source TEXT
            );
        """)

    async def write_integrations(self, integrations: list[dict]):
        """Write integrations to the database (upsert)."""
        if not self._pool or not integrations:
            return

        _LOGGER.debug(
            "[writer.write_integrations] Writing %d integrations to database...",
            len(integrations),
        )
        try:
            text_fields = ["entry_id", "domain", "title", "state", "source"]
            for integration in integrations:
                for field in text_fields:
                    if integration.get(field) is not None:
                        integration[field] = str(integration[field]).replace("\0", "")

            rows = [
                (
                    i.get("entry_id"),
                    i.get("domain"),
                    i.get("title"),
                    i.get("state"),
                    i.get("source"),
                )
                for i in integrations
            ]

            await self._execute_many(
                """
                INSERT INTO integrations (entry_id, domain, title, state, source)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (entry_id) DO UPDATE SET
                    domain = EXCLUDED.domain,
                    title = EXCLUDED.title,
                    state = EXCLUDED.state,
                    source = EXCLUDED.source;
            """,
                rows,
            )
            _LOGGER.debug(
                "[writer.write_integrations] Integrations written successfully"
            )
        except Exception as exc:
            _LOGGER.error(
                "[writer.write_integrations] Error writing %d integrations: %s (%s)",
                len(integrations),
                exc,
                type(exc).__name__,
                exc_info=True,
            )

    async def _check_timescaledb_available(self):
        """Warn once if the database is plain PostgreSQL.

        Scribe keeps working — the tables are ordinary ones — but chunking,
        compression and the size sensors all silently do nothing, which looks
        exactly like Scribe being broken rather than the extension missing.
        """
        try:
            installed = await self._fetchval(
                "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'timescaledb')"
            )
        except Exception as e:
            _LOGGER.debug(
                "[writer._check_timescaledb_available] Extension check failed: %s (%s)",
                e,
                type(e).__name__,
            )
            return

        if installed:
            self._clear_issue(ISSUE_NO_TIMESCALEDB)
            return

        _LOGGER.warning(
            "[writer._check_timescaledb_available] TimescaleDB extension is not installed: "
            "history is recorded, but chunking and compression are unavailable."
        )
        self._report_issue(
            ISSUE_NO_TIMESCALEDB,
            "no_timescaledb",
            {"host": _safe_target(self.db_url)},
            severity=ir.IssueSeverity.WARNING,
        )

    async def _init_hypertable(self, table_name, segment_by):
        """Initialize hypertable and compression.

        Each operation is done in its own transaction to avoid
        'transaction aborted' errors when one operation fails.
        """

        # Convert to hypertable
        try:
            _LOGGER.debug(
                "[writer._init_hypertable] Converting %s to hypertable (chunk=%s)...",
                table_name,
                self.chunk_interval,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"SELECT create_hypertable('{table_name}', 'time', chunk_time_interval => INTERVAL '{self.chunk_interval}', if_not_exists => TRUE);"
                )
        except Exception as e:
            _LOGGER.warning(
                "[writer._init_hypertable] Hypertable creation failed for %s (chunk=%s) — might not be TimescaleDB or already exists: %s (%s)",
                table_name,
                self.chunk_interval,
                e,
                type(e).__name__,
            )

        # Enable compression
        try:
            _LOGGER.debug(
                "[writer._init_hypertable] Enabling compression for %s (segment_by=%s)...",
                table_name,
                segment_by,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(f"""
                    ALTER TABLE {table_name} SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = '{segment_by}',
                        timescaledb.compress_orderby = 'time DESC'
                    );
                """)
        except Exception as e:
            _LOGGER.debug(
                "[writer._init_hypertable] Compression enable failed for %s: %s (%s)",
                table_name,
                e,
                type(e).__name__,
            )

        # Add compression policy
        try:
            _LOGGER.debug(
                "[writer._init_hypertable] Adding compression policy for %s (after=%s)...",
                table_name,
                self.compress_after,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"SELECT add_compression_policy('{table_name}', INTERVAL '{self.compress_after}', if_not_exists => TRUE);"
                )
        except Exception as e:
            _LOGGER.debug(
                "[writer._init_hypertable] Compression policy failed for %s (after=%s): %s (%s)",
                table_name,
                self.compress_after,
                e,
                type(e).__name__,
            )

    # ------------------------------------------------------------------
    # Sanitization
    # ------------------------------------------------------------------

    def _sanitize_obj(self, obj: Any, depth: int = 0) -> Any:
        try:
            if depth > 100:
                return str(obj)

            if obj is None or isinstance(obj, (bool, int)):
                return obj

            if isinstance(obj, (dt_datetime, date)):
                return obj

            if isinstance(obj, float):
                if math.isinf(obj) or math.isnan(obj):
                    return None
                return obj

            if isinstance(obj, str):
                if "\0" in obj:
                    _LOGGER.warning(
                        "[writer._sanitize_obj] Sanitized string containing null byte: %r",
                        obj,
                    )
                    return obj.replace("\0", "")
                return obj
            if isinstance(obj, dict):
                return {k: self._sanitize_obj(v, depth + 1) for k, v in obj.items()}
            if isinstance(obj, list):
                return [self._sanitize_obj(v, depth + 1) for v in obj]
            if isinstance(obj, tuple):
                return tuple(self._sanitize_obj(v, depth + 1) for v in obj)

            # Non-JSON-native types: convert to something the upstream
            # JSONEncoder can handle. Dataclasses → dict (preserves field
            # names), everything else (UUIDs, integration-specific objects
            # like TargetChannelInfo, …) → str. Without this, json.dumps
            # crashes on the whole batch — see issue #35.
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return self._sanitize_obj(dataclasses.asdict(obj), depth + 1)
            return str(obj)
        except Exception as e:
            _LOGGER.error(
                "[writer._sanitize_obj] Error sanitizing object (type=%s, depth=%d): %s (%s)",
                type(obj).__name__,
                depth,
                e,
                type(e).__name__,
                exc_info=True,
            )
            return str(obj)

    # ------------------------------------------------------------------
    # Flush / write batch
    # ------------------------------------------------------------------

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
                        if x["type"] == "state":
                            for field in ["entity_id", "state"]:
                                if x.get(field) is not None:
                                    x[field] = str(x[field]).replace("\0", "")

                            states_res.append(x)
                        elif x["type"] == "event":
                            for field in [
                                "event_type",
                                "origin",
                                "context_id",
                                "context_user_id",
                                "context_parent_id",
                            ]:
                                if x.get(field) is not None:
                                    x[field] = str(x[field]).replace("\0", "")

                            events_res.append(x)
                    return states_res, events_res

                # Run CPU-intensive serialization in executor
                states_data, events_data = await self.hass.async_add_executor_job(
                    _process_batch, batch
                )

                # Held from metadata_id resolution through the COPY: a concurrent
                # rename must not move/delete a metadata_id in between, or the
                # copied rows would be stranded under a deleted id.
                async with self._metadata_lock:
                    # Resolve Metadata IDs for states
                    if states_data:
                        eids = set()
                        for s in states_data:
                            if "entity_id" in s:
                                eids.add(s["entity_id"])

                        if eids:
                            await self._ensure_metadata_ids(list(eids))

                        final_states_data = []
                        for s in states_data:
                            eid = s.pop("entity_id", None)
                            if eid and eid in self._entity_id_map:
                                s["metadata_id"] = self._entity_id_map[eid]
                                final_states_data.append(s)
                            else:
                                _LOGGER.warning(
                                    "[writer._flush] Skipping state for unknown entity_id: %r (not in cache — INSERT into entities may have failed)",
                                    eid,
                                )

                        # states_raw is keyed by (metadata_id, time). Home
                        # Assistant can emit two states for one entity at the
                        # same instant — a restored state alongside a live one,
                        # or a force_update — and COPY has no ON CONFLICT, so a
                        # single duplicate would abort the whole batch. Since
                        # the batch would then be re-buffered and fail again on
                        # every retry, that is a permanent stall, not a hiccup.
                        # Keep the last state seen for each key.
                        deduped = {}
                        for s in final_states_data:
                            deduped[(s["metadata_id"], s["time"])] = s
                        if len(deduped) != len(final_states_data):
                            _LOGGER.debug(
                                "[writer._flush] Dropped %d state(s) sharing a "
                                "(metadata_id, time) key within the batch",
                                len(final_states_data) - len(deduped),
                            )
                        states_data = list(deduped.values())

                    async with self._pool.acquire() as conn:
                        async with conn.transaction():
                            if states_data:
                                await self._copy_records(
                                    conn=conn,
                                    table_name="states_raw",
                                    columns=[
                                        "time",
                                        "metadata_id",
                                        "state",
                                        "value",
                                        "attributes",
                                    ],
                                    records=[
                                        (
                                            s["time"],
                                            s["metadata_id"],
                                            s.get("state"),
                                            s.get("value"),
                                            s.get("attributes"),
                                        )
                                        for s in states_data
                                    ],
                                    conflict_target="metadata_id, time",
                                )
                            if events_data:
                                await self._copy_records(
                                    conn=conn,
                                    table_name=self.table_name_events,
                                    columns=[
                                        "time",
                                        "event_type",
                                        "event_data",
                                        "origin",
                                        "context_id",
                                        "context_user_id",
                                        "context_parent_id",
                                    ],
                                    records=[
                                        (
                                            e["time"],
                                            e["event_type"],
                                            e.get("event_data"),
                                            e.get("origin"),
                                            e.get("context_id"),
                                            e.get("context_user_id"),
                                            e.get("context_parent_id"),
                                        )
                                        for e in events_data
                                    ],
                                )

                duration = time.time() - start_time
                self._states_written += len(states_data)
                self._events_written += len(events_data)

                self._states_history.append((time.time(), len(states_data)))
                self._events_history.append((time.time(), len(events_data)))

                self._last_write_duration = duration

                if not self._connected:
                    _LOGGER.info(
                        "[writer._flush] Database connection restored. Flushed %d states and %d events.",
                        len(states_data),
                        len(events_data),
                    )

                self._connected = True
                self._last_error = None

                # Writing works again: retire whatever the failure path raised.
                if self._consecutive_flush_failures:
                    self._consecutive_flush_failures = 0
                    self._clear_issue(ISSUE_WRITE_FAILING)
                    self._clear_issue(ISSUE_DB_UNREACHABLE)
                if len(self._queue) < self.max_queue_size:
                    self._clear_issue(ISSUE_BUFFER_FULL)

            except asyncpg.PostgresError as e:
                msg = str(e)
                if "\n" in msg:
                    msg = msg.split("\n")[0]

                sqlstate = getattr(e, "sqlstate", None)
                _LOGGER.error(
                    "[writer._flush] PostgreSQL error during flush (type=%s, sqlstate=%s, batch_size=%d): %s",
                    type(e).__name__,
                    sqlstate,
                    len(batch),
                    msg,
                    exc_info=True,
                )

                self._connected = False
                self._last_error = msg
                self._note_flush_failure(f"{msg} (sqlstate={sqlstate})")

                if self.buffer_on_failure:
                    _LOGGER.warning(
                        "[writer._flush] Buffering %d items due to PostgreSQL failure (sqlstate=%s). Current queue size: %d/%d",
                        len(batch),
                        sqlstate,
                        len(self._queue),
                        self.max_queue_size,
                    )

            except Exception as e:
                _LOGGER.error(
                    "[writer._flush] Unexpected error flushing batch (batch_size=%d): %s (%s)",
                    len(batch),
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                self._connected = False
                self._last_error = str(e)
                self._note_flush_failure(f"{e} ({type(e).__name__})")

                if self.buffer_on_failure:
                    _LOGGER.warning(
                        "[writer._flush] Buffering %d items due to failure. Current queue size: %d/%d",
                        len(batch),
                        len(self._queue),
                        self.max_queue_size,
                    )
                    self._queue = deque(
                        batch + list(self._queue), maxlen=self.max_queue_size
                    )

                    if len(self._queue) == self.max_queue_size:
                        _LOGGER.warning(
                            "[writer._flush] Buffer full! Queue size: %d (max=%d) — oldest items will be dropped",
                            len(self._queue),
                            self.max_queue_size,
                        )
                        # From here on history is being lost, silently.
                        self._report_issue(
                            ISSUE_BUFFER_FULL,
                            "buffer_full",
                            {"max_queue_size": str(self.max_queue_size)},
                            severity=ir.IssueSeverity.ERROR,
                        )
                else:
                    self._dropped_events += len(batch)
                    _LOGGER.warning(
                        "[writer._flush] Dropped %d items (buffering disabled, total dropped since start=%d)",
                        len(batch),
                        self._dropped_events,
                    )
                    self._report_issue(
                        ISSUE_DATA_DROPPED,
                        "data_dropped",
                        {"dropped": str(self._dropped_events)},
                        severity=ir.IssueSeverity.ERROR,
                    )
        except Exception as e:
            _LOGGER.error(
                "[writer._flush] Critical error in flush routine: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Entity rename
    # ------------------------------------------------------------------

    async def rename_entity(self, old_entity_id: str, new_entity_id: str):
        """Rename an entity in the database (metadata only).

        ``states_raw`` references entities through the surrogate ``metadata_id``
        (a foreign key to ``entities.id``), so a rename is a single cheap UPDATE on
        the small ``entities`` table — no history rows are moved or rewritten, and we
        never touch the compressed ``states_raw`` hypertable.

        Collision handling: the target ``new_entity_id`` may already exist in
        ``entities``. That row is **not** automatically assumed to be dead — with
        entity_id "musical chairs" (A → clim while B leaves clim) or a Scribe event
        backlog, it can still belong to a *live* entity, and clobbering it would
        corrupt/misroute that entity's data. So we only take the name over when the
        occupant is a *provably dead orphan*: its registry coordinates
        (``unique_id``, ``domain``, ``platform``) are all known and resolve to
        nothing in Home Assistant's live entity registry. In that case the orphan is
        *reused*: its ``states_raw`` rows are folded into the renamed entity's
        ``metadata_id`` and its ``entities`` row is deleted, so the renamed entity
        carries one continuous history under the new name (typical case: the same
        physical device re-added with a new ``unique_id``). This is the one path
        that rewrites ``states_raw`` rows — proportional to the orphan's history
        size, inside the rename transaction.
        If the occupant is still live — or we cannot prove it is dead (missing
        ``unique_id``/``domain``/``platform``) — we refuse and leave everything
        untouched (safe no-op).
        """
        if not self._pool:
            return

        _LOGGER.info(
            "[writer.rename_entity] Renaming entity %s -> %s",
            old_entity_id,
            new_entity_id,
        )

        merged_orphan_rows = None
        dropped_duplicate_rows = 0
        merge_reason = None
        # Serialized with write_entities and the flush metadata section — see
        # _metadata_lock. Held through cache invalidation so no flush can
        # resolve entity_ids against a half-renamed state.
        await self._metadata_lock.acquire()
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    try:
                        # Fast path — target name is free. Wrapped in a SAVEPOINT so a
                        # unique-violation does not abort the surrounding transaction
                        # (Postgres marks a tx as failed after any error).
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
                                new_entity_id,
                                old_entity_id,
                            )
                    except asyncpg.UniqueViolationError:
                        # The destination name is occupied in Scribe. Only take it over
                        # if that row is a PROVABLY DEAD orphan; never clobber a row that
                        # may still belong to a live entity (id "musical chairs" / event
                        # backlog), which would corrupt that entity's data.
                        occ = await conn.fetchrow(
                            "SELECT id, unique_id, domain, platform FROM entities WHERE entity_id = $1",
                            new_entity_id,
                        )
                        src = await conn.fetchrow(
                            "SELECT id, unique_id FROM entities WHERE entity_id = $1",
                            old_entity_id,
                        )
                        if occ is None:
                            # Freed between the failed UPDATE and now — just rename.
                            await conn.execute(
                                "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
                                new_entity_id,
                                old_entity_id,
                            )
                        elif src is None:
                            # The UniqueViolation proved this row existed moments
                            # ago; only a concurrent delete can land here.
                            _LOGGER.warning(
                                "[writer.rename_entity] Source %s vanished during "
                                "rename to %s; nothing to do.",
                                old_entity_id,
                                new_entity_id,
                            )
                            return
                        else:
                            # Death is only provable when all three registry
                            # coordinates are known — a partial row (e.g. created by
                            # _ensure_metadata_ids before the registry sync filled it
                            # in) could resolve to nothing while its entity is alive.
                            occupant_live_eid = None
                            provable = (
                                occ["unique_id"] and occ["domain"] and occ["platform"]
                            )
                            if provable:
                                reg = er.async_get(self.hass)
                                occupant_live_eid = reg.async_get_entity_id(
                                    occ["domain"],
                                    occ["platform"],
                                    occ["unique_id"],
                                )
                            if (
                                occ["unique_id"]
                                and occ["unique_id"] == src["unique_id"]
                            ):
                                # Self-collision: both rows carry the same unique_id,
                                # so they are two rows for ONE entity — a concurrent
                                # metadata sync inserted the destination row before
                                # this rename ran. Merging is unconditionally safe.
                                # (The live registry cannot tell this case apart from
                                # a different entity legitimately living at the
                                # destination: in both, the occupant's unique_id
                                # resolves to new_entity_id. Only the stored
                                # unique_ids distinguish them.) The _metadata_lock
                                # prevents new occurrences; this heals rows left by
                                # earlier versions.
                                merge_reason = "self-collision (row created by a concurrent metadata sync)"
                            elif occupant_live_eid is not None or not provable:
                                # Occupant is a *different* live entity, or cannot be
                                # proven dead: refuse. Nothing is modified (safe
                                # no-op), but the user must hear about it — their
                                # history is now split across the two ids.
                                _LOGGER.error(
                                    "[writer.rename_entity] Refusing %s -> %s: destination is "
                                    "not a provably-dead orphan (live_entity=%s, unique_id=%s, "
                                    "domain=%s, platform=%s). "
                                    "Left unchanged to avoid corrupting a live entity.",
                                    old_entity_id,
                                    new_entity_id,
                                    occupant_live_eid,
                                    occ["unique_id"],
                                    occ["domain"],
                                    occ["platform"],
                                )
                                if occupant_live_eid is not None:
                                    self._report_rename_issue(
                                        new_entity_id,
                                        "rename_refused_live",
                                        {
                                            "old_entity_id": old_entity_id,
                                            "new_entity_id": new_entity_id,
                                            "occupant": occupant_live_eid,
                                        },
                                    )
                                else:
                                    self._report_rename_issue(
                                        new_entity_id,
                                        "rename_refused_unprovable",
                                        {
                                            "old_entity_id": old_entity_id,
                                            "new_entity_id": new_entity_id,
                                        },
                                    )
                                return
                            else:
                                # Provably dead orphan: reuse it (typical case: the
                                # same device re-added with a new unique_id).
                                merge_reason = "dead orphan"
                            # Fold the occupant's history into the renamed entity's
                            # metadata_id, drop its row, then rename — one
                            # continuous history under the new name.
                            #
                            # states_raw's primary key is (metadata_id, time), so
                            # any occupant row at a timestamp the surviving entity
                            # already holds would violate it and abort the whole
                            # rename. Both rows describe the same instant of what
                            # is now one entity: the survivor's own row wins and
                            # the duplicate is dropped first.
                            dropped = await conn.execute(
                                """
                                DELETE FROM states_raw o
                                WHERE o.metadata_id = $2
                                  AND EXISTS (
                                      SELECT 1 FROM states_raw l
                                      WHERE l.metadata_id = $1 AND l.time = o.time
                                  )
                                """,
                                src["id"],
                                occ["id"],
                            )
                            status = await conn.execute(
                                "UPDATE states_raw SET metadata_id = $1 WHERE metadata_id = $2",
                                src["id"],
                                occ["id"],
                            )
                            merged_orphan_rows = _affected_rows(status)
                            dropped_duplicate_rows = _affected_rows(dropped)
                            await conn.execute(
                                "DELETE FROM entities WHERE id = $1",
                                occ["id"],
                            )
                            await conn.execute(
                                "UPDATE entities SET entity_id = $1 WHERE entity_id = $2",
                                new_entity_id,
                                old_entity_id,
                            )

            # Invalidate the touched cache entries; they are re-resolved lazily from
            # the DB on the next write (rename is rare, so the extra lookup is free).
            for eid in (old_entity_id, new_entity_id):
                mid = self._entity_id_map.pop(eid, None)
                if mid is not None:
                    self._metadata_id_map.pop(mid, None)

            # A rename to this name succeeded — retire any repair issue a previous
            # refused/failed attempt on the same destination may have raised.
            self._clear_rename_issue(new_entity_id)

            if merged_orphan_rows is not None:
                _LOGGER.warning(
                    "[writer.rename_entity] Renamed entity %s -> %s: destination was a "
                    "%s; reused it by merging %s history rows into the "
                    "renamed entity and deleting the occupant metadata row%s.",
                    old_entity_id,
                    new_entity_id,
                    merge_reason,
                    "an unknown number of"
                    if merged_orphan_rows < 0
                    else merged_orphan_rows,
                    f" ({dropped_duplicate_rows} duplicate rows at timestamps the "
                    "renamed entity already had were dropped)"
                    if dropped_duplicate_rows
                    else "",
                )
            else:
                _LOGGER.info(
                    "[writer.rename_entity] Renamed entity %s -> %s successfully",
                    old_entity_id,
                    new_entity_id,
                )

        except Exception as e:
            _LOGGER.error(
                "[writer.rename_entity] Failed to rename entity %s -> %s: %s (%s)",
                old_entity_id,
                new_entity_id,
                e,
                type(e).__name__,
                exc_info=True,
            )
            # The registry event will not fire again: without this issue the
            # rename is silently lost and the entity's history splits.
            self._report_rename_issue(
                new_entity_id,
                "rename_failed",
                {
                    "old_entity_id": old_entity_id,
                    "new_entity_id": new_entity_id,
                    "error": f"{e} ({type(e).__name__})",
                },
                severity=ir.IssueSeverity.ERROR,
            )
        finally:
            self._metadata_lock.release()

    def _note_flush_failure(self, error: str):
        """Count a failed flush and raise an issue once it stops looking transient.

        A single failure is a blip — a database restart, a brief network drop —
        and the next flush heals it. Repeated ones mean recording has stopped,
        which otherwise shows up nowhere but the log.
        """
        self._consecutive_flush_failures += 1
        if self._consecutive_flush_failures == WRITE_FAILURE_ISSUE_THRESHOLD:
            self._report_issue(
                ISSUE_WRITE_FAILING,
                "write_failing",
                {
                    "failures": str(self._consecutive_flush_failures),
                    "error": error,
                },
                severity=ir.IssueSeverity.ERROR,
            )

    def _report_issue(
        self,
        issue_id: str,
        translation_key: str,
        placeholders: dict[str, str] | None = None,
        severity: "ir.IssueSeverity" = ir.IssueSeverity.WARNING,
    ):
        """Surface a condition in the Repairs dashboard (best effort).

        Issues are keyed by `issue_id`, so a repeating condition updates one
        entry instead of piling up, and whoever resolves it calls
        `_clear_issue` with the same id. Never let UI plumbing break a write.
        """
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=severity,
                translation_key=translation_key,
                translation_placeholders=placeholders or {},
                learn_more_url=ISSUE_LEARN_MORE_URL,
            )
        except Exception as e:  # never let UI plumbing break the writer
            _LOGGER.debug(
                "[writer._report_issue] Could not create repair issue %s: %s (%s)",
                issue_id,
                e,
                type(e).__name__,
            )

    def _clear_issue(self, issue_id: str):
        """Retire an issue once its condition no longer holds (best effort)."""
        try:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception as e:
            _LOGGER.debug(
                "[writer._clear_issue] Could not delete repair issue %s: %s (%s)",
                issue_id,
                e,
                type(e).__name__,
            )

    def _report_rename_issue(
        self,
        new_entity_id: str,
        translation_key: str,
        placeholders: dict[str, str],
        severity: "ir.IssueSeverity" = ir.IssueSeverity.WARNING,
    ):
        """Report a rename problem, keyed by the destination entity_id.

        Repeated attempts on the same name update one issue, and any later
        successful rename to that name retires it.
        """
        self._report_issue(
            f"rename_collision_{new_entity_id}",
            translation_key,
            placeholders,
            severity,
        )

    def _clear_rename_issue(self, new_entity_id: str):
        """Delete the repair issue for this destination, if one exists."""
        self._clear_issue(f"rename_collision_{new_entity_id}")

    # ------------------------------------------------------------------
    # Query / stats
    # ------------------------------------------------------------------

    async def query(self, sql: str):
        """Execute a read-only query against the database."""
        if not self._pool:
            raise RuntimeError("Database not connected")

        _LOGGER.debug("[writer.query] Executing query (Read-Only): %s", sql)
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL TRANSACTION READ ONLY")
                    try:
                        rows = await conn.fetch(sql)
                        return [dict(row) for row in rows]
                    except Exception:
                        raise
        except Exception as e:
            sqlstate = getattr(e, "sqlstate", None)
            _LOGGER.error(
                "[writer.query] Error executing query (sqlstate=%s, type=%s): %s | SQL=%s",
                sqlstate,
                type(e).__name__,
                e,
                sql,
                exc_info=True,
            )
            raise

    async def get_db_stats(self, stats_type: str = "all"):
        """Fetch database statistics using TimescaleDB chunks view.

        Args:
            stats_type: Type of stats to fetch - "chunk", "size", or "all"
        """
        stats = {}
        if not self._pool:
            return stats

        tasks = []

        async def get_states_chunk_stats():
            try:
                row = await self._fetchrow("""
                    SELECT 
                        COUNT(*) AS total_chunks,
                        SUM(CASE WHEN is_compressed THEN 1 ELSE 0 END) AS compressed_chunks,
                        SUM(CASE WHEN NOT is_compressed THEN 1 ELSE 0 END) AS uncompressed_chunks
                    FROM timescaledb_information.chunks
                    WHERE hypertable_name = 'states_raw'
                """)
                if row:
                    return {
                        "states_total_chunks": row["total_chunks"] or 0,
                        "states_compressed_chunks": row["compressed_chunks"] or 0,
                        "states_uncompressed_chunks": row["uncompressed_chunks"] or 0,
                    }
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:states_chunk] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )
            return {}

        async def get_states_size_stats():
            total_bytes = 0
            compressed_bytes = 0
            before_bytes = 0
            after_bytes = 0

            try:
                total_bytes = (
                    await self._fetchval(
                        "SELECT total_bytes FROM hypertable_detailed_size('states_raw')"
                    )
                    or 0
                )
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:states_total_size] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            try:
                compressed_bytes = (
                    await self._fetchval(
                        "SELECT after_compression_total_bytes FROM hypertable_compression_stats('states_raw')"
                    )
                    or 0
                )
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:states_compressed_size] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            try:
                row = await self._fetchrow(
                    "SELECT before_compression_total_bytes, after_compression_total_bytes FROM hypertable_compression_stats('states_raw')"
                )
                if row:
                    before_bytes = row["before_compression_total_bytes"] or 0
                    after_bytes = row["after_compression_total_bytes"] or 0
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:states_compression_ratio] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            return {
                "states_total_size": total_bytes,
                "states_compressed_size": compressed_bytes,
                "states_uncompressed_size": max(0, total_bytes - compressed_bytes),
                "states_before_compression_total_bytes": before_bytes,
                "states_after_compression_total_bytes": after_bytes,
            }

        async def get_events_chunk_stats():
            try:
                row = await self._fetchrow(f"""
                    SELECT 
                        COUNT(*) AS total_chunks,
                        SUM(CASE WHEN is_compressed THEN 1 ELSE 0 END) AS compressed_chunks,
                        SUM(CASE WHEN NOT is_compressed THEN 1 ELSE 0 END) AS uncompressed_chunks
                    FROM timescaledb_information.chunks
                    WHERE hypertable_name = '{self.table_name_events}'
                """)
                if row:
                    return {
                        "events_total_chunks": row["total_chunks"] or 0,
                        "events_compressed_chunks": row["compressed_chunks"] or 0,
                        "events_uncompressed_chunks": row["uncompressed_chunks"] or 0,
                    }
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:events_chunk] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )
            return {}

        async def get_events_size_stats():
            total_bytes = 0
            compressed_bytes = 0
            before_bytes = 0
            after_bytes = 0

            try:
                total_bytes = (
                    await self._fetchval(
                        f"SELECT total_bytes FROM hypertable_detailed_size('{self.table_name_events}')"
                    )
                    or 0
                )
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:events_total_size] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            try:
                compressed_bytes = (
                    await self._fetchval(
                        f"SELECT after_compression_total_bytes FROM hypertable_compression_stats('{self.table_name_events}')"
                    )
                    or 0
                )
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:events_compressed_size] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            try:
                row = await self._fetchrow(
                    f"SELECT before_compression_total_bytes, after_compression_total_bytes FROM hypertable_compression_stats('{self.table_name_events}')"
                )
                if row:
                    before_bytes = row["before_compression_total_bytes"] or 0
                    after_bytes = row["after_compression_total_bytes"] or 0
            except Exception as e:
                _LOGGER.debug(
                    "[writer.get_db_stats:events_compression_ratio] Failed: %s (%s)",
                    e,
                    type(e).__name__,
                )

            return {
                "events_total_size": total_bytes,
                "events_compressed_size": compressed_bytes,
                "events_uncompressed_size": max(0, total_bytes - compressed_bytes),
                "events_before_compression_total_bytes": before_bytes,
                "events_after_compression_total_bytes": after_bytes,
            }

        if self.record_states:
            if stats_type in ("chunk", "all"):
                tasks.append(get_states_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(get_states_size_stats())

        if self.record_events:
            if stats_type in ("chunk", "all"):
                tasks.append(get_events_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(get_events_size_stats())

        if tasks:
            results = await asyncio.gather(*tasks)
            for result in results:
                stats.update(result)

        return stats

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

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
