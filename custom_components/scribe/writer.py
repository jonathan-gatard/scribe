"""Database writer for Scribe.

This module handles the asynchronous writing of data to the TimescaleDB database.
It implements an asyncio-based writer that buffers events and writes them in batches
to minimize database connection overhead and blocking.

NOTE: This version uses asyncpg directly (no SQLAlchemy) to avoid the greenlet
dependency which is not available on Python 3.14 / Alpine Linux (Home Assistant OS).
"""

import logging
import asyncio
from dataclasses import dataclass
import re
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
from datetime import date, datetime as dt_datetime, timedelta
from decimal import Decimal

import asyncpg

from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    DEFAULT_BATCH_SIZE,
    DEFAULT_BUFFER_ON_FAILURE,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_DB_SSL,
    DEFAULT_ENABLE_AREAS,
    DEFAULT_ENABLE_DEVICES,
    DEFAULT_ENABLE_INTEGRATIONS,
    DEFAULT_ENABLE_STATS_IO,
    DEFAULT_ENABLE_USERS,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_RECORD_STATES,
    DEFAULT_RETENTION_EVENTS,
    DEFAULT_RETENTION_STATES,
    DEFAULT_TABLE_NAME_EVENTS,
    DEFAULT_TABLE_NAME_STATES,
)

_LOGGER = logging.getLogger(__name__)


def _create_ssl_context(
    ssl_root_cert=None, ssl_cert_file=None, ssl_key_file=None
) -> tuple[ssl.SSLContext, list[str]]:
    """Create and configure SSL context in executor thread.

    asyncpg calls ssl.load_cert_chain() synchronously when establishing SSL connections.
    By creating the SSLContext here (in an executor thread) and passing it to asyncpg,
    we avoid blocking Home Assistant's event loop.

    This function must be run via hass.async_add_executor_job().

    Returns:
        (context, problems) — `problems` lists what was configured but could
        not be applied, so the caller can say so instead of connecting with
        less protection than the user asked for.
    """
    _LOGGER.debug(
        "[writer._create_ssl_context] Creating SSL context in executor thread..."
    )

    problems: list[str] = []
    ssl_context = ssl.create_default_context()

    try:
        ssl_context.load_default_certs()
        _LOGGER.debug("[writer._create_ssl_context] Loaded system CA certificates")
    except Exception as e:
        _LOGGER.warning(
            "[writer._create_ssl_context] Could not load system CA certificates: %s (%s) — continuing with built-in defaults",
            e,
            type(e).__name__,
        )

    # Client certificate (mutual TLS). Failing to load it does not stop the
    # connection — it just makes it an ordinary one, which is why it is
    # reported rather than logged: the server may well accept it, and nobody
    # would learn that the client authentication they configured is not
    # happening.
    if ssl_cert_file:
        if not Path(ssl_cert_file).exists():
            _LOGGER.warning(
                "[writer._create_ssl_context] SSL cert file configured but not found: %s — connection will proceed without client certificate",
                ssl_cert_file,
            )
            problems.append(f"client certificate not found: {ssl_cert_file}")
        else:
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
                problems.append(
                    f"client certificate {ssl_cert_file} could not be loaded: "
                    f"{e} ({type(e).__name__})"
                )

    # CA certificate for verifying the server. Note that failing to load a
    # private CA does not disable verification — the system CAs above are still
    # in force — so the usual outcome is a connection that refuses to establish
    # at all, which is a much better failure than a silent one.
    if ssl_root_cert:
        if not Path(ssl_root_cert).exists():
            _LOGGER.warning(
                "[writer._create_ssl_context] SSL root cert configured but not found: %s — falling back to the system CA store",
                ssl_root_cert,
            )
            problems.append(f"CA certificate not found: {ssl_root_cert}")
        else:
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
                problems.append(
                    f"CA certificate {ssl_root_cert} could not be loaded: "
                    f"{e} ({type(e).__name__})"
                )

    _LOGGER.debug("[writer._create_ssl_context] SSL context created successfully")
    return ssl_context, problems


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
ISSUE_SCHEMA_FAILED = "schema_failed"
ISSUE_SSL_DEGRADED = "ssl_degraded"
ISSUE_VIEW_FAILED = "view_failed"
ISSUE_LEGACY_SCHEMA = "legacy_schema"
# Per-table: states and events can degrade independently.
ISSUE_NO_HYPERTABLE = "no_hypertable_{table}"
ISSUE_NO_COMPRESSION = "no_compression_{table}"

# Last version able to convert a pre-3.0 database. Named in the log line and
# in the Repairs issue, so both stay right if it ever moves.
LEGACY_MIGRATION_VERSION = "3.8"
# Per-table: states and events carry their own retention setting, so each one
# reports (and retires) its own failure.
ISSUE_RETENTION_FAILED = "retention_failed_{table}"

# How many consecutive failed flushes before bothering the user: a single
# failure is a blip (a restart, a brief network drop) and self-heals.
WRITE_FAILURE_ISSUE_THRESHOLD = 3

# How long to wait between connection attempts while the database is down.
# Doubles from the first to the second, so a database that is merely slow to
# boot is picked up in seconds while one that is down for hours is left alone.
RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 300

# Ceiling for the `scribe.query` service, in milliseconds. Long enough for a
# genuine report over a year of history, short enough that a runaway query
# cannot hold a pooled connection and hammer the server indefinitely.
QUERY_TIMEOUT_MS = 120_000


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
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(
            f"Invalid table name '{name}': only letters, digits, and underscores are allowed"
        )
    return name


# A PostgreSQL interval literal, restricted to what a retention policy needs:
# one or more "<number> <unit>" pairs. Retention values are interpolated into
# SQL and every match is dropped chunks, so anything else is refused outright.
_INTERVAL_UNIT = r"(?:second|minute|hour|day|week|month|year)s?"
_INTERVAL_RE = re.compile(
    rf"^\s*\d+\s*{_INTERVAL_UNIT}(?:\s+\d+\s*{_INTERVAL_UNIT})*\s*$",
    re.IGNORECASE,
)


async def ensure_timescaledb(conn) -> bool:
    """Make sure the TimescaleDB extension is usable, enabling it if we can.

    Scribe is a TimescaleDB integration: without the extension, chunking,
    compression, retention and every size sensor do nothing. The most common
    reason it is missing is simply a forgotten `CREATE EXTENSION` — and a
    database user with CREATE on the database (what the documented setup grants)
    can run it themselves, so Scribe does it rather than making the user go
    read the logs to find out why half of it is inert.

    Returns True if the extension is installed once this returns. Never raises:
    a database that says no is a `False`, which the caller turns into either a
    refused setup or a Repairs issue.
    """
    try:
        if await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'timescaledb')"
        ):
            return True
    except Exception as e:
        _LOGGER.debug(
            "[writer.ensure_timescaledb] Extension check failed: %s (%s)",
            e,
            type(e).__name__,
        )
        return False

    try:
        available = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_available_extensions "
            "WHERE name = 'timescaledb')"
        )
    except Exception as e:
        _LOGGER.debug(
            "[writer.ensure_timescaledb] Availability check failed: %s (%s)",
            e,
            type(e).__name__,
        )
        return False

    if not available:
        _LOGGER.debug(
            "[writer.ensure_timescaledb] The TimescaleDB extension is not "
            "installed on this PostgreSQL server, so it cannot be enabled."
        )
        return False

    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except Exception as e:
        _LOGGER.debug(
            "[writer.ensure_timescaledb] Could not enable the extension "
            "(needs CREATE on the database): %s (%s)",
            e,
            type(e).__name__,
        )
        return False

    _LOGGER.info(
        "[writer.ensure_timescaledb] TimescaleDB was available but not enabled "
        "on this database — Scribe enabled it."
    )
    return True


_ENTITY_COLUMNS = (
    "entity_id",
    "unique_id",
    "platform",
    "domain",
    "name",
    "device_id",
    "area_id",
    "capabilities",
)


def _entity_row(entity: dict) -> tuple:
    """The entities-table row for one registry entry, in column order."""
    return tuple(entity.get(column) for column in _ENTITY_COLUMNS)


def _entity_unchanged(row, entity: dict) -> bool:
    """True when the stored row already matches the registry entry.

    entity_id is what the row was looked up by, so only the rest is compared.
    """
    return all(row[column] == entity.get(column) for column in _ENTITY_COLUMNS[1:])


def _partition_entities(entities: list[dict], existing: dict) -> tuple[list, list]:
    """Split registry entries into rows to insert and rows to update.

    Entries whose stored row is already identical are in neither list: writing
    them back would burn a SERIAL id and dirty a page for nothing.
    """
    to_insert: list[tuple] = []
    to_update: list[tuple] = []
    for entity in entities:
        eid = entity.get("entity_id")
        if not eid:
            continue
        row = existing.get(eid)
        if row is None:
            to_insert.append(_entity_row(entity))
        elif not _entity_unchanged(row, entity):
            to_update.append(_entity_row(entity))
    return to_insert, to_update


# Returned by `_sanitize_scalar` for anything that is not a leaf value. A
# sentinel rather than None, which is itself a perfectly good sanitized value.
# Used on the fast path and again on both collision paths.
_RENAME_ENTITY_SQL = "UPDATE entities SET entity_id = $1 WHERE entity_id = $2"

_NOT_SCALAR = object()


def _sanitize_key(key) -> str:
    """Make a mapping key something jsonb can actually hold.

    Two ways an attribute key kills a write, both fatal to the *whole* batch
    and both permanent — the batch is re-buffered and fails again on every
    retry. A null byte, which PostgreSQL refuses in a key exactly as in a
    value ("\\u0000 cannot be converted to text"), and a key that is not a
    string — a tuple, say — which `json.dumps` refuses outright. Values were
    already cleaned before reaching the codec; keys were not.
    """
    if not isinstance(key, str):
        key = str(key)
    return key.replace("\0", "")


def _validate_interval(value: str) -> str:
    """Validate an interval string before it reaches SQL.

    Raises ValueError if the value is not a plain "<number> <unit>" interval.
    """
    if not _INTERVAL_RE.fullmatch(value or ""):
        raise ValueError(
            f"Invalid interval '{value}': expected something like '30 days', "
            "'6 months' or '1 year'"
        )
    return value.strip()


@dataclass(frozen=True)
class WriterConfig:
    """Everything the writer needs, resolved once by setup.

    Passed as one object rather than as twenty-three parameters: the call sites
    were long enough that a misplaced argument was easy to write and hard to
    see, and every default lives here instead of being repeated at each of them.
    """

    db_url: str
    chunk_interval: str = DEFAULT_CHUNK_TIME_INTERVAL
    compress_after: str = DEFAULT_COMPRESS_AFTER
    retention_states: str = DEFAULT_RETENTION_STATES
    retention_events: str = DEFAULT_RETENTION_EVENTS
    record_states: bool = DEFAULT_RECORD_STATES
    record_events: bool = DEFAULT_RECORD_EVENTS
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval: int = DEFAULT_FLUSH_INTERVAL
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE
    buffer_on_failure: bool = DEFAULT_BUFFER_ON_FAILURE
    table_name_states: str = DEFAULT_TABLE_NAME_STATES
    table_name_events: str = DEFAULT_TABLE_NAME_EVENTS
    use_ssl: bool = DEFAULT_DB_SSL
    ssl_root_cert: str | None = None
    ssl_cert_file: str | None = None
    ssl_key_file: str | None = None
    enable_table_areas: bool = DEFAULT_ENABLE_AREAS
    enable_table_devices: bool = DEFAULT_ENABLE_DEVICES
    enable_table_integrations: bool = DEFAULT_ENABLE_INTEGRATIONS
    enable_table_users: bool = DEFAULT_ENABLE_USERS
    enable_stats_io: bool = DEFAULT_ENABLE_STATS_IO


class ScribeWriter:
    """Handle database connections and writing.

    This class runs as an asyncio task. It maintains a queue of events to be written.
    Data is flushed to the database when the queue reaches BATCH_SIZE or when
    FLUSH_INTERVAL seconds have passed.

    Uses asyncpg directly (no SQLAlchemy) to avoid the greenlet dependency.
    """

    def __init__(self, hass: HomeAssistant, config: WriterConfig):
        """Initialize the writer."""
        self.hass = hass
        self.config = config

        # Normalize DSN - strip SQLAlchemy dialect prefix if present
        self.db_url = _normalize_dsn(config.db_url)

        self.chunk_interval = config.chunk_interval
        self.compress_after = config.compress_after
        # Empty = no retention policy. Non-empty = Scribe keeps the policy on
        # its own tables in sync with this value, dropping chunks older than it.
        self.retention_states = (config.retention_states or "").strip()
        self.retention_events = (config.retention_events or "").strip()
        self.record_states = config.record_states
        self.record_events = config.record_events
        self.batch_size = config.batch_size
        self.flush_interval = config.flush_interval
        self.max_queue_size = config.max_queue_size
        self.buffer_on_failure = config.buffer_on_failure
        self.table_name_states = _validate_table_name(config.table_name_states)
        self.table_name_events = _validate_table_name(config.table_name_events)
        self.use_ssl = config.use_ssl
        self.ssl_root_cert = config.ssl_root_cert
        self.ssl_cert_file = config.ssl_cert_file
        self.ssl_key_file = config.ssl_key_file
        self.enable_table_areas = config.enable_table_areas
        self.enable_table_devices = config.enable_table_devices
        self.enable_table_integrations = config.enable_table_integrations
        self.enable_table_users = config.enable_table_users
        self.enable_stats_io = config.enable_stats_io

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
        self._queue: deque = deque(maxlen=config.max_queue_size)
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
        # Reconnection backoff, used while the database is unreachable.
        self._connect_delay = RECONNECT_MIN_DELAY
        self._next_connect_attempt = 0.0
        # Strong references to fire-and-forget flush tasks (see `enqueue`).
        self._background_tasks: set[asyncio.Task] = set()
        # Resolved once per start, before the hypertable steps run.
        self._has_timescaledb = False
        # Set when the database predates Scribe 3.0: nothing is recorded until
        # it is converted, so there is no point queuing anything either.
        self._legacy_blocked = False

        # asyncpg connection pool (replaces SQLAlchemy engine)
        self._pool: asyncpg.Pool = None
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

    async def _build_ssl_context(self):
        """Return the ssl argument for `create_pool`: a context, or False.

        Certificate paths may be given relative to the Home Assistant config
        directory, and building the context reads files from disk — so it runs
        in an executor rather than on the event loop.
        """
        if not self.use_ssl:
            return False

        def resolve_path(path_str):
            if not path_str:
                return None
            path = Path(path_str)
            if not path.is_absolute():
                return str(Path(self.hass.config.config_dir) / path)
            return str(path)

        _LOGGER.debug("[writer.start] SSL enabled, creating SSL context in executor...")
        context, problems = await self.hass.async_add_executor_job(
            _create_ssl_context,
            resolve_path(self.ssl_root_cert),
            resolve_path(self.ssl_cert_file),
            resolve_path(self.ssl_key_file),
        )

        if problems:
            # The connection will still be encrypted; what the user configured
            # on top of that is not in force. Saying so is the whole point —
            # a client certificate that never loads looks exactly like one that
            # works, right up until an audit.
            self._report_issue(
                ISSUE_SSL_DEGRADED,
                "ssl_degraded",
                {"problems": "; ".join(problems)},
                severity=ir.IssueSeverity.WARNING,
            )
        else:
            self._clear_issue(ISSUE_SSL_DEGRADED)

        return context

    async def _connect(self) -> bool:
        """Create the connection pool and build the schema. False if unreachable.

        Used both by `start()` and by the writer loop: a database that is not
        up yet when Home Assistant boots — the two often start together — must
        not leave Scribe dead until the next restart.
        """
        try:
            _LOGGER.debug(
                "[writer.start] Creating asyncpg pool for %s",
                _safe_target(self.db_url),
            )

            ssl_arg = await self._build_ssl_context()

            async def _init_connection(conn):
                # Home Assistant attributes are dicts; encoding them through
                # asyncpg's own jsonb codec avoids a round trip through Python
                # string serialization on every row.
                await conn.set_type_codec(
                    "jsonb",
                    encoder=lambda x: (
                        b"\x01"
                        + json.dumps(x, cls=JSONEncoder, default=_json_default).encode(
                            "utf-8"
                        )
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
            # Nothing is being recorded until this is fixed, and the only other
            # signal is a log line at startup.
            self._report_issue(
                ISSUE_DB_UNREACHABLE,
                "db_unreachable",
                {
                    "host": _safe_target(self.db_url),
                    "error": f"{e} ({type(e).__name__})",
                },
                severity=ir.IssueSeverity.ERROR,
            )
            self._pool = None
            self._engine = None
            return False

        await self.init_db()
        return self._connected

    async def _ensure_connected(self) -> bool:
        """Reconnect if needed, no more often than the backoff allows.

        The backoff doubles up to RECONNECT_MAX_DELAY so a database that is
        down for an hour is not hammered every flush interval, while one that
        is merely slow to boot is picked up within seconds.
        """
        if self._pool is not None:
            return True

        now = time.time()
        if now < self._next_connect_attempt:
            return False

        self._next_connect_attempt = now + self._connect_delay
        if await self._connect():
            _LOGGER.info(
                "[writer._ensure_connected] Reconnected to %s — %d buffered item(s) will be written",
                _safe_target(self.db_url),
                len(self._queue),
            )
            self._connect_delay = RECONNECT_MIN_DELAY
            return True

        self._connect_delay = min(self._connect_delay * 2, RECONNECT_MAX_DELAY)
        _LOGGER.debug(
            "[writer._ensure_connected] Still unreachable; next attempt in %ss (%d item(s) buffered)",
            self._connect_delay,
            len(self._queue),
        )
        return False

    async def start(self):
        """Start the writer task.

        A database that cannot be reached does not stop the writer: the loop
        keeps buffering and retries the connection, so history recorded while
        the database was down is written once it returns.
        """
        try:
            if self._running:
                return

            _LOGGER.debug("[writer.start] Starting ScribeWriter...")
            self._running = True

            if self._pool is None:
                await self._connect()
            else:
                # A pool handed in (a restart, or a test): the schema still has
                # to be checked, which _connect would otherwise have done.
                await self.init_db()

            self._task = asyncio.create_task(self._run())
            if self._pool is None:
                _LOGGER.warning(
                    "[writer.start] Database unreachable at startup — buffering and retrying in the background."
                )
            else:
                _LOGGER.info("[writer.start] ScribeWriter started successfully")

        except Exception as e:
            _LOGGER.error(
                "[writer.start] Unexpected error starting ScribeWriter: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )
            raise

    async def _row_count(self, relation: str) -> int:
        """Exact row count for a relation.

        There is no cheap shortcut here. TimescaleDB's approximate_row_count()
        looks tempting — 21 ms against a full scan — but it derives compressed
        chunks from `reltuples`, which counts *batches* and assumes each is
        full. Measured on a real chunk: 1 270 000 estimated against 444 968
        actual, 2.85x too high. A "total written" counter that overstates by
        nearly three times is worse than a slow one, so the exact count stands
        and the cost is avoided by not asking unless someone is looking (see
        _get_initial_counts).
        """
        return await self._fetchval(f"SELECT count(*) FROM {relation}") or 0

    async def _get_initial_counts(self):
        """Seed the written-rows counters from the database, if anyone reads them.

        These two numbers exist only to feed the I/O statistics sensors, which
        are opt-in and off by default. Counting them means aggregating every
        row across every chunk — 90 million rows over 103 compressed chunks on
        a real installation — so an install that does not display them should
        not pay for them at every Home Assistant start.
        """
        if not self.enable_stats_io:
            _LOGGER.debug(
                "[writer._get_initial_counts] I/O statistics are disabled; "
                "skipping the initial row counts."
            )
            return

        _LOGGER.debug("[writer._get_initial_counts] Fetching initial row counts...")
        try:
            if self.record_states:
                self._states_written = await self._row_count(self.table_name_states)

            if self.record_events:
                self._events_written = await self._row_count(self.table_name_events)

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
            # Raised, not swallowed: resolving an entity is part of writing its
            # state, not a best-effort extra. Returning here left every state
            # of an unregistered entity skipped as "unknown entity_id" while
            # the flush reported success — the batch was cleared and those
            # states were gone. The caller re-buffers instead, and the next
            # attempt registers them.
            raise

    async def stop(self):
        """Stop the writer task."""
        _LOGGER.debug("[writer.stop] Stopping ScribeWriter...")
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Not re-raised on purpose: this is the cancellation *we* just
                # requested two lines up, not one aimed at `stop()`. Letting it
                # out would abort Home Assistant's unload for no reason.
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

        # Fetch initial counts (background - might take a while on large DBs)
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
                # Nothing can be written without a pool, and flushing would
                # just re-buffer the batch; reconnect first, on a backoff.
                if not await self._ensure_connected():
                    self._warn_if_buffer_full()
                    continue
                await self._flush()
            # No `except asyncio.CancelledError` here on purpose: it derives
            # from BaseException, so the handler below never sees it and the
            # cancellation `stop()` requests propagates — a loop that caught it
            # and broke out would end up reporting itself as completed.
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
            if not self._running or self._legacy_blocked:
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
                # Keep a strong reference: the event loop only holds weak
                # ones, so a fire-and-forget task can be garbage-collected
                # while it runs — here that would drop a batch already
                # drained out of the queue, with nothing raised anywhere.
                task = asyncio.create_task(self._flush())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
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

    async def _create_tables(self, conn):
        """Create every table this configuration asks for, in dependency order."""
        # entities FIRST: the states view depends on it, and runtime state
        # writes always upsert into it.
        await self._init_entities_table(conn)

        for enabled, create in (
            (self.enable_table_users, self._init_users_table),
            (self.enable_table_areas, self._init_areas_table),
            (self.enable_table_devices, self._init_devices_table),
            (self.enable_table_integrations, self._init_integrations_table),
            # states and events come after entities exists
            (self.record_states, self._init_states_table),
            (self.record_events, self._init_events_table),
        ):
            if enabled:
                await create(conn)

    async def _init_hypertables(self):
        """Convert and tune each recorded table, one failure never stopping another.

        Each table gets its own try: a states_raw that cannot be converted must
        not leave events unchunked as well.
        """
        for enabled, table, segment_by, retention in (
            (self.record_states, "states_raw", "metadata_id", self.retention_states),
            (
                self.record_events,
                self.table_name_events,
                "event_type",
                self.retention_events,
            ),
        ):
            if not enabled:
                continue
            try:
                await self._init_hypertable(table, segment_by, retention)
            except Exception as e:
                _LOGGER.error(
                    "[writer.init_db] Failed to init hypertable/compression for %s (chunk=%s, compress_after=%s): %s (%s)",
                    table,
                    self.chunk_interval,
                    self.compress_after,
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

    async def init_db(self):
        """Initialize database tables."""
        _LOGGER.debug("[writer.init_db] Initializing database...")
        if not self._pool:
            _LOGGER.warning(
                "[writer.init_db] No connection pool available, skipping DB initialization"
            )
            return

        try:
            # 1. Refuse to touch a pre-3.0 database (see _detect_legacy_schema)
            async with self._pool.acquire() as conn:
                legacy = await self._detect_legacy_schema(conn)
            if legacy:
                self._block_on_legacy_schema(legacy)
                return

            # 2. Create tables (own transaction)
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await self._create_tables(conn)

            # Whether the storage features exist at all decides both what to
            # attempt below and whether a failure means anything to the user.
            self._has_timescaledb = await self._check_timescaledb_available()

            await self._init_hypertables()

            _LOGGER.info("[writer.init_db] Database initialized successfully")
            self._connected = True
            self._clear_issue(ISSUE_DB_UNREACHABLE)
            self._clear_issue(ISSUE_SCHEMA_FAILED)
            # The database was converted (or replaced) since the last start.
            self._clear_issue(ISSUE_LEGACY_SCHEMA)

        except Exception as e:
            _LOGGER.error(
                "[writer.init_db] Error initializing database: %s (%s)",
                e,
                type(e).__name__,
                exc_info=True,
            )
            self._connected = False
            # The database answers but Scribe could not build its schema —
            # typically missing privileges. Nothing is recorded, and the only
            # other sign is one line in a log nobody reads on a good day.
            self._report_issue(
                ISSUE_SCHEMA_FAILED,
                "schema_failed",
                {
                    "host": _safe_target(self.db_url),
                    "error": f"{e} ({type(e).__name__})",
                },
                severity=ir.IssueSeverity.ERROR,
            )

    async def _detect_legacy_schema(self, conn) -> str | None:
        """Name the pre-3.0 artifact found in the database, or None.

        Scribe 3.0 replaced the `states` table with `states_raw` plus a view,
        and gave `entities` a SERIAL primary key. Converting a database from
        the old layout is what 3.x shipped and 3.9 dropped: the code carried
        an unbounded backfill, a 60-second startup delay and a compression
        dance for a path essentially nobody is still on. Rather than half-run
        it, Scribe now stops and points at the version that can finish the job.

        Detection has to be conservative in both directions — recording is
        refused on a hit, and a miss on a legacy `entities` table would let
        writes fail row by row instead.
        """
        # On 3.x `states` is a view over states_raw. A base table by that name
        # is the pre-3.0 history itself.
        if await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'states' AND table_type = 'BASE TABLE')"
        ):
            return "states"

        # An older Scribe renamed `states` and was interrupted before (or
        # during) the backfill: the history is there, but nothing reads it.
        if await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'states_legacy')"
        ):
            return "states_legacy"

        # `entities` keyed by entity_id text instead of a SERIAL id. Writes
        # resolve metadata_ids through that column, so this one is fatal.
        if await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'entities')"
        ) and not await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.columns "
            "WHERE table_name = 'entities' AND column_name = 'id')"
        ):
            return "entities"

        return None

    def _block_on_legacy_schema(self, relation: str):
        """Stop recording and explain how to convert the database.

        Nothing is created, renamed or dropped: the old data stays exactly
        where it is, so installing 3.8 still converts it cleanly. Recording is
        refused rather than half-done — writing into a schema Scribe cannot
        fully build would strand new states in tables the rest of the code
        cannot read.
        """
        self._legacy_blocked = True
        self._connected = False
        _LOGGER.error(
            "[writer._block_on_legacy_schema] Pre-3.0 database detected (`%s`): "
            "this version of Scribe cannot convert it and is recording nothing. "
            "Install Scribe %s, let it run until the migration finishes, then "
            "update again. Your data is untouched.",
            relation,
            LEGACY_MIGRATION_VERSION,
        )
        self._report_issue(
            ISSUE_LEGACY_SCHEMA,
            "legacy_schema",
            {"relation": relation, "version": LEGACY_MIGRATION_VERSION},
            severity=ir.IssueSeverity.ERROR,
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

        # 2. The view is created separately: `_init_states_view` refuses to
        # replace a *table* that happens to carry the configured name.
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
                    "[writer._init_states_view] '%s' is a table, not a view — leaving it alone rather than dropping it.",
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
            self._clear_issue(ISSUE_VIEW_FAILED)
        except Exception as e:
            _LOGGER.error(
                "[writer._init_states_view] Failed to create view '%s' over 'states_raw': %s (%s)",
                self.table_name_states,
                e,
                type(e).__name__,
                exc_info=True,
            )
            # States keep being recorded into states_raw, but every query,
            # dashboard and example in the documentation goes through this
            # view: without it the history looks lost.
            self._report_issue(
                ISSUE_VIEW_FAILED,
                "view_failed",
                {
                    "view": self.table_name_states,
                    "error": f"{e} ({type(e).__name__})",
                },
                severity=ir.IssueSeverity.ERROR,
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

    def _sanitize_entity_rows(self, entities: list[dict]):
        """Strip null bytes from the text columns and flatten capabilities."""
        for entity in entities:
            for field in _ENTITY_COLUMNS[:-1]:
                if entity.get(field) is not None:
                    entity[field] = str(entity[field]).replace("\0", "")
            if entity.get("capabilities"):
                entity["capabilities"] = self._sanitize_obj(entity["capabilities"])

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
            self._sanitize_entity_rows(entities)

            entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]
            if not entity_ids:
                return

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
                    to_insert, to_update = _partition_entities(entities, existing)

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

    async def _check_timescaledb_available(self) -> bool:
        """Warn once if the database is plain PostgreSQL.

        Scribe keeps working — the tables are ordinary ones — but chunking,
        compression and the size sensors all silently do nothing, which looks
        exactly like Scribe being broken rather than the extension missing.

        The answer is also what tells the hypertable steps whether a failure is
        worth reporting: on plain PostgreSQL they are *expected* to fail, and
        this issue already explains why.
        """
        try:
            async with self._pool.acquire() as conn:
                installed = await ensure_timescaledb(conn)
        except Exception as e:
            _LOGGER.debug(
                "[writer._check_timescaledb_available] Extension check failed: %s (%s)",
                e,
                type(e).__name__,
            )
            return False

        if installed:
            self._clear_issue(ISSUE_NO_TIMESCALEDB)
            return True

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
        return False

    async def _init_hypertable(self, table_name, segment_by, retention: str = ""):
        """Initialize hypertable, compression and retention.

        Each operation is done in its own transaction to avoid
        'transaction aborted' errors when one operation fails.

        On plain PostgreSQL every step here is bound to fail, and
        `no_timescaledb` already explains that once: they are skipped rather
        than retried into the log at each start. Retention still runs, so a
        configured retention that cannot be applied is still reported.
        """
        if not self._has_timescaledb:
            self._clear_issue(ISSUE_NO_HYPERTABLE.format(table=table_name))
            self._clear_issue(ISSUE_NO_COMPRESSION.format(table=table_name))
            await self._apply_retention_policy(table_name, retention)
            return

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

        # Chunk size and compression policy, kept in sync with the settings
        await self._apply_chunk_interval(table_name)
        await self._apply_compression_policy(table_name)

        # Report what the database *ended up with*, not what was attempted:
        # every step above swallows its own error, and a table that silently
        # stayed a plain one grows several times faster than the user expects.
        await self._verify_storage_features(table_name)

        # Retention (drops chunks) — always last, and reported to the user
        # when it fails, unlike the best-effort steps above.
        await self._apply_retention_policy(table_name, retention)

    async def _verify_storage_features(self, table_name: str):
        """Check that chunking and compression are actually in place.

        Only called when TimescaleDB is installed, so a missing feature here
        means something specific went wrong — most often that the Scribe
        database user does not own the table it is writing to.
        """
        hypertable_issue = ISSUE_NO_HYPERTABLE.format(table=table_name)
        compression_issue = ISSUE_NO_COMPRESSION.format(table=table_name)

        try:
            is_hypertable = await self._fetchval(
                "SELECT EXISTS (SELECT FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = $1)",
                table_name,
            )
        except Exception as e:
            _LOGGER.debug(
                "[writer._verify_storage_features] %s: could not verify: %s (%s)",
                table_name,
                e,
                type(e).__name__,
            )
            return

        if not is_hypertable:
            _LOGGER.warning(
                "[writer._verify_storage_features] %s is not a hypertable although "
                "TimescaleDB is installed: no chunking, no compression, no retention.",
                table_name,
            )
            self._report_issue(
                hypertable_issue,
                "no_hypertable",
                {"table": table_name},
                severity=ir.IssueSeverity.WARNING,
            )
            # Compression is a property of a hypertable: reporting its absence
            # too would just be the same problem said twice.
            self._clear_issue(compression_issue)
            return

        self._clear_issue(hypertable_issue)

        try:
            has_policy = await self._fetchval(
                "SELECT EXISTS (SELECT FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression' AND hypertable_name = $1)",
                table_name,
            )
        except Exception as e:
            _LOGGER.debug(
                "[writer._verify_storage_features] %s: could not verify the "
                "compression policy: %s (%s)",
                table_name,
                e,
                type(e).__name__,
            )
            return

        if has_policy:
            self._clear_issue(compression_issue)
            return

        _LOGGER.warning(
            "[writer._verify_storage_features] %s has no compression policy: "
            "history is chunked but never compressed, and the database will grow "
            "several times larger than it needs to.",
            table_name,
        )
        self._report_issue(
            compression_issue,
            "no_compression",
            {"table": table_name, "compress_after": self.compress_after},
            severity=ir.IssueSeverity.WARNING,
        )

    async def _apply_chunk_interval(self, table_name: str):
        """Keep the hypertable's chunk size in sync with `chunk_time_interval`.

        `create_hypertable(..., if_not_exists => TRUE)` silently ignores its
        arguments once the table exists, so before this the setting only ever
        applied on the very first start — changing it later did nothing while
        the log claimed otherwise.

        Only *future* chunks are affected; chunks already written keep the span
        they were created with. Nothing is rewritten, moved or lost.
        """
        try:
            # NULL when the relation is not a hypertable (plain PostgreSQL):
            # fetchval returns None and there is nothing to keep in sync.
            unchanged = await self._fetchval(
                """
                SELECT time_interval = $2::text::interval
                FROM timescaledb_information.dimensions
                WHERE hypertable_name = $1 AND column_name = 'time'
                """,
                table_name,
                self.chunk_interval,
            )
            if unchanged is None or unchanged:
                return

            await self._execute(
                "SELECT set_chunk_time_interval($1::regclass, $2::text::interval)",
                table_name,
                self.chunk_interval,
            )
            _LOGGER.info(
                "[writer._apply_chunk_interval] %s: chunk size is now %s — this "
                "applies to new chunks; existing ones keep their current span",
                table_name,
                self.chunk_interval,
            )
        except Exception as e:
            _LOGGER.debug(
                "[writer._apply_chunk_interval] %s: could not set chunk interval "
                "'%s': %s (%s)",
                table_name,
                self.chunk_interval,
                e,
                type(e).__name__,
            )

    async def _apply_compression_policy(self, table_name: str):
        """Keep the compression policy in sync with `compress_after`.

        `add_compression_policy(..., if_not_exists => TRUE)` skips a policy that
        already exists, *including* one with a different interval, so the value
        used to be frozen at whatever the first start created. Replacing the
        policy is not destructive: chunks already compressed stay compressed,
        and the policy only decides when the next ones are.
        """
        try:
            current = await self._fetchval(
                """
                SELECT config ->> 'compress_after'
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_compression' AND hypertable_name = $1
                """,
                table_name,
            )

            if current is not None:
                unchanged = await self._fetchval(
                    "SELECT $1::text::interval = $2::text::interval",
                    current,
                    self.compress_after,
                )
                if unchanged:
                    return
                await self._execute(
                    "SELECT remove_compression_policy($1::regclass, if_exists => true)",
                    table_name,
                )

            await self._execute(
                "SELECT add_compression_policy($1::regclass, $2::text::interval)",
                table_name,
                self.compress_after,
            )
            _LOGGER.info(
                "[writer._apply_compression_policy] %s: chunks are compressed "
                "once they are older than %s",
                table_name,
                self.compress_after,
            )
        except Exception as e:
            _LOGGER.debug(
                "[writer._apply_compression_policy] %s: could not apply "
                "compression policy '%s': %s (%s)",
                table_name,
                self.compress_after,
                e,
                type(e).__name__,
            )

    async def _apply_retention_policy(self, table_name: str, retention: str):
        """Keep the table's retention policy in sync with the configured value.

        Scribe owns the retention policy on its own tables: an empty setting
        means no policy, so one found there is removed — otherwise clearing the
        field in the UI would leave chunks being dropped with no way back.

        A retention policy *deletes history*, so a failure here is surfaced in
        Repairs rather than logged and forgotten: the user asked for a
        bounded database and needs to know they did not get one.
        """
        issue_id = ISSUE_RETENTION_FAILED.format(table=table_name)

        try:
            if retention:
                _validate_interval(retention)
        except ValueError as e:
            _LOGGER.error(
                "[writer._apply_retention_policy] %s: refusing to apply retention: %s",
                table_name,
                e,
            )
            self._report_issue(
                issue_id,
                "retention_failed",
                {"table": table_name, "retention": retention, "error": str(e)},
                severity=ir.IssueSeverity.ERROR,
            )
            return

        try:
            # `drop_after` is what the policy was created with; comparing as an
            # interval avoids re-creating the job on every restart just because
            # TimescaleDB spells "1 month" as "1 mon".
            current = await self._fetchval(
                """
                SELECT config ->> 'drop_after'
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_retention'
                  AND hypertable_name = $1
                """,
                table_name,
            )

            if not retention:
                if current:
                    _LOGGER.warning(
                        "[writer._apply_retention_policy] %s: removing the existing retention policy (was dropping chunks older than %s) — no retention is configured in Scribe",
                        table_name,
                        current,
                    )
                    await self._execute(
                        f"SELECT remove_retention_policy('{table_name}', if_exists => true)"
                    )
                self._clear_issue(issue_id)
                return

            if current:
                # Cast through text: with a bare `$1::interval` asyncpg infers
                # an interval parameter and rejects the string outright.
                unchanged = await self._fetchval(
                    "SELECT $1::text::interval = $2::text::interval",
                    current,
                    retention,
                )
                if unchanged:
                    _LOGGER.debug(
                        "[writer._apply_retention_policy] %s: retention policy already set to %s",
                        table_name,
                        retention,
                    )
                    self._clear_issue(issue_id)
                    return
                await self._execute(
                    f"SELECT remove_retention_policy('{table_name}', if_exists => true)"
                )

            await self._execute(
                f"SELECT add_retention_policy('{table_name}', INTERVAL '{retention}')"
            )
            _LOGGER.warning(
                "[writer._apply_retention_policy] %s: retention policy set — data older than %s will be DELETED permanently",
                table_name,
                retention,
            )
            self._clear_issue(issue_id)

        except Exception as e:
            if not retention:
                # Nothing configured means nothing to remove, so failing to
                # even look is not a problem worth a line: on plain
                # PostgreSQL `timescaledb_information` does not exist, and
                # neither does any policy.
                _LOGGER.debug(
                    "[writer._apply_retention_policy] %s: no retention configured, "
                    "and no policy could be inspected: %s (%s)",
                    table_name,
                    e,
                    type(e).__name__,
                )
                return

            _LOGGER.error(
                "[writer._apply_retention_policy] %s: could not apply retention '%s': %s (%s)",
                table_name,
                retention,
                e,
                type(e).__name__,
                exc_info=True,
            )
            self._report_issue(
                issue_id,
                "retention_failed",
                {
                    "table": table_name,
                    "retention": retention,
                    "error": f"{e} ({type(e).__name__})",
                },
                severity=ir.IssueSeverity.ERROR,
            )

    # ------------------------------------------------------------------
    # Sanitization
    # ------------------------------------------------------------------

    def _sanitize_scalar(self, obj: Any) -> Any:
        """Sanitize a single value, or return `_NOT_SCALAR` for a container.

        Split out from `_sanitize_obj` so each half reads as one decision:
        what a leaf becomes, and how a tree is walked.
        """
        if obj is None or isinstance(obj, (bool, int)):
            return obj

        if isinstance(obj, (dt_datetime, date)):
            return obj

        if isinstance(obj, float):
            # jsonb has no way to spell inf/nan, and asyncpg raises on them.
            return None if (math.isinf(obj) or math.isnan(obj)) else obj

        # Numbers PostgreSQL hands back in types JSON has no room for.
        # `numeric` arrives as Decimal — from EXTRACT(EPOCH …), avg() and any
        # ::numeric — and `interval` as timedelta. Both used to end up as
        # strings here and as an unserializable response from `scribe.query`;
        # as numbers they are usable in a template either way.
        if isinstance(obj, Decimal):
            return float(obj)

        if isinstance(obj, timedelta):
            return obj.total_seconds()

        if isinstance(obj, str):
            if "\0" in obj:
                _LOGGER.warning(
                    "[writer._sanitize_obj] Sanitized string containing null byte: %r",
                    obj,
                )
                return obj.replace("\0", "")
            return obj

        return _NOT_SCALAR

    def _sanitize_obj(self, obj: Any, depth: int = 0) -> Any:
        try:
            scalar = self._sanitize_scalar(obj)
            if scalar is not _NOT_SCALAR:
                return scalar

            # Past the depth guard every remaining case falls through to
            # str(obj): a structure this deep is either cyclic or not worth
            # walking, and recursing further risks the stack.
            if depth <= 100:
                if isinstance(obj, dict):
                    return {
                        _sanitize_key(k): self._sanitize_obj(v, depth + 1)
                        for k, v in obj.items()
                    }
                if isinstance(obj, (list, tuple)):
                    values = [self._sanitize_obj(v, depth + 1) for v in obj]
                    return tuple(values) if isinstance(obj, tuple) else values

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

    def _warn_if_buffer_full(self):
        """Say so when a disconnection has filled the buffer.

        Without this the queue silently drops its oldest items once it reaches
        `max_queue_size`, and the only visible sign of a long outage would be
        a hole in the history.
        """
        if len(self._queue) < self.max_queue_size:
            return
        _LOGGER.warning(
            "[writer._run] Buffer full while the database is unreachable: queue size %d (max=%d) — oldest items are being dropped",
            len(self._queue),
            self.max_queue_size,
        )
        self._report_issue(
            ISSUE_BUFFER_FULL,
            "buffer_full",
            {"max_queue_size": str(self.max_queue_size)},
            severity=ir.IssueSeverity.ERROR,
        )

    def _prune_rate_history(self):
        """Drop rate samples older than the 60-second window they average over."""
        now = time.time()
        while self._states_history and now - self._states_history[0][0] > 60:
            self._states_history.popleft()
        while self._events_history and now - self._events_history[0][0] > 60:
            self._events_history.popleft()

    def _split_batch(self, batch_items):
        """Sanitize a batch and split it into states and events.

        Runs in an executor: sanitizing is pure CPU work over every value of
        every item, and doing it on the event loop stalls Home Assistant.
        """
        states_res = []
        events_res = []

        for item in (self._sanitize_obj(i) for i in batch_items):
            if item["type"] == "state":
                fields = ("entity_id", "state")
                target = states_res
            elif item["type"] == "event":
                fields = (
                    "event_type",
                    "origin",
                    "context_id",
                    "context_user_id",
                    "context_parent_id",
                )
                target = events_res
            else:
                continue

            for field in fields:
                if item.get(field) is not None:
                    item[field] = str(item[field]).replace("\0", "")
            target.append(item)

        return states_res, events_res

    async def _resolve_state_metadata_ids(self, states_data):
        """Turn queued states into the rows COPY writes, resolving their entity.

        Must run under `_metadata_lock`: a concurrent rename may move or delete
        a metadata_id, and the COPY that follows would strand its rows.

        The queued items are read, never written: a failed COPY re-buffers those
        very dicts, and an earlier version resolved them by popping `entity_id`
        off each one — which made every state of the retry unresolvable, dropped
        as "unknown entity" on the way back in. Building the rows here instead
        of copying each dict also keeps the batch allocation-free.
        """
        eids = {s["entity_id"] for s in states_data if "entity_id" in s}
        if eids:
            await self._ensure_metadata_ids(list(eids))

        # states_raw is keyed by (metadata_id, time). Home Assistant can emit
        # two states for one entity at the same instant — a restored state
        # alongside a live one, or a force_update — and COPY has no ON CONFLICT,
        # so a single duplicate would abort the whole batch. Since the batch
        # would then be re-buffered and fail again on every retry, that is a
        # permanent stall, not a hiccup. Keying by (metadata_id, time) keeps the
        # last state seen for each.
        rows = {}
        skipped = 0
        for state in states_data:
            eid = state.get("entity_id")
            metadata_id = self._entity_id_map.get(eid) if eid else None
            if metadata_id is None:
                skipped += 1
                _LOGGER.warning(
                    "[writer._flush] Skipping state for unknown entity_id: %r (not in cache — INSERT into entities may have failed)",
                    eid,
                )
                continue
            when = state["time"]
            rows[(metadata_id, when)] = (
                when,
                metadata_id,
                state.get("state"),
                state.get("value"),
                state.get("attributes"),
            )

        dropped = len(states_data) - skipped - len(rows)
        if dropped:
            _LOGGER.debug(
                "[writer._flush] Dropped %d state(s) sharing a "
                "(metadata_id, time) key within the batch",
                dropped,
            )
        return list(rows.values())

    async def _copy_batch(self, state_rows, events_data):
        """Write both halves of a batch in one transaction.

        `state_rows` are already the tuples COPY takes (see
        `_resolve_state_metadata_ids`); events still carry their dicts, since
        nothing has to be resolved for them.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if state_rows:
                    await self._copy_records(
                        conn=conn,
                        table_name="states_raw",
                        columns=["time", "metadata_id", "state", "value", "attributes"],
                        records=state_rows,
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

    def _record_flush_success(self, states_written, events_written, duration):
        """Update the counters and retire whatever a previous failure raised."""
        self._states_written += states_written
        self._events_written += events_written

        now = time.time()
        self._states_history.append((now, states_written))
        self._events_history.append((now, events_written))
        self._last_write_duration = duration

        if not self._connected:
            _LOGGER.info(
                "[writer._flush] Database connection restored. Flushed %d states and %d events.",
                states_written,
                events_written,
            )

        self._connected = True
        self._last_error = None

        if self._consecutive_flush_failures:
            self._consecutive_flush_failures = 0
            self._clear_issue(ISSUE_WRITE_FAILING)
            self._clear_issue(ISSUE_DB_UNREACHABLE)
        if len(self._queue) < self.max_queue_size:
            self._clear_issue(ISSUE_BUFFER_FULL)

    def _handle_flush_failure(self, batch, message, detail):
        """Keep a batch that could not be written, or account for dropping it.

        Both failure paths land here on purpose. They used to be written twice,
        and the server-side one — the very case buffering exists for, a full
        disk or a revoked grant — logged "Buffering N items" while dropping
        them: the re-buffering line was only ever in the other branch.
        """
        self._connected = False
        self._last_error = message
        self._note_flush_failure(detail)

        if not self.buffer_on_failure:
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
            return

        _LOGGER.warning(
            "[writer._flush] Buffering %d items due to a write failure. Current queue size: %d/%d",
            len(batch),
            len(self._queue),
            self.max_queue_size,
        )
        self._queue = deque(batch + list(self._queue), maxlen=self.max_queue_size)

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

    async def _flush(self):
        """Flush the queue to the database."""
        try:
            self._flush_pending = False  # Reset flag immediately

            # Prune first, so the rolling window stays honest even while idle.
            self._prune_rate_history()

            if not self._queue:
                return

            # Swap queue - drain the deque
            batch = list(self._queue)
            self._queue.clear()

            start_time = time.time()

            try:
                states_data, events_data = await self.hass.async_add_executor_job(
                    self._split_batch, batch
                )

                # Held from metadata_id resolution through the COPY: a concurrent
                # rename must not move/delete a metadata_id in between, or the
                # copied rows would be stranded under a deleted id.
                async with self._metadata_lock:
                    if states_data:
                        states_data = await self._resolve_state_metadata_ids(
                            states_data
                        )
                    await self._copy_batch(states_data, events_data)

                self._record_flush_success(
                    len(states_data), len(events_data), time.time() - start_time
                )

            except asyncpg.PostgresError as e:
                msg = str(e).split("\n", maxsplit=1)[0]
                sqlstate = getattr(e, "sqlstate", None)
                _LOGGER.error(
                    "[writer._flush] PostgreSQL error during flush (type=%s, sqlstate=%s, batch_size=%d): %s",
                    type(e).__name__,
                    sqlstate,
                    len(batch),
                    msg,
                    exc_info=True,
                )
                self._handle_flush_failure(batch, msg, f"{msg} (sqlstate={sqlstate})")

            except Exception as e:
                _LOGGER.error(
                    "[writer._flush] Unexpected error flushing batch (batch_size=%d): %s (%s)",
                    len(batch),
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                self._handle_flush_failure(batch, str(e), f"{e} ({type(e).__name__})")
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

    def _classify_rename_collision(self, occ, src):
        """Decide what the occupant of the destination name is.

        Returns ``(merge_reason, occupant_live_entity_id)``. A merge_reason of
        None means refuse: the occupant is a *different* live entity, or its
        registry coordinates are incomplete so its death cannot be proven — and
        a partial row (one `_ensure_metadata_ids` created before the registry
        sync filled it in) would resolve to nothing while its entity is very
        much alive.
        """
        occupant_live_eid = None
        provable = occ["unique_id"] and occ["domain"] and occ["platform"]
        if provable:
            occupant_live_eid = er.async_get(self.hass).async_get_entity_id(
                occ["domain"], occ["platform"], occ["unique_id"]
            )

        if occ["unique_id"] and occ["unique_id"] == src["unique_id"]:
            # Self-collision: both rows carry the same unique_id, so they are
            # two rows for ONE entity — a concurrent metadata sync inserted the
            # destination row before this rename ran. Merging is unconditionally
            # safe. (The live registry cannot tell this case apart from a
            # different entity legitimately living at the destination: in both,
            # the occupant's unique_id resolves to new_entity_id. Only the
            # stored unique_ids distinguish them.) The _metadata_lock prevents
            # new occurrences; this heals rows left by earlier versions.
            return (
                "self-collision (row created by a concurrent metadata sync)",
                occupant_live_eid,
            )

        if occupant_live_eid is not None or not provable:
            return None, occupant_live_eid

        # Provably dead orphan: reuse it (typical case: the same device
        # re-added with a new unique_id).
        return "dead orphan", occupant_live_eid

    async def _take_over_occupied_name(self, conn, old_entity_id, new_entity_id):
        """Resolve a rename whose destination name is already taken.

        Returns ``(merge_reason, merged_rows, dropped_rows)`` once the
        destination belongs to the renamed entity, or ``None`` when the rename
        is refused — in which case nothing has been modified.
        """
        merged_orphan_rows = None
        dropped_duplicate_rows = 0
        merge_reason = None
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
                _RENAME_ENTITY_SQL,
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
            return None  # refused: nothing was modified
        else:
            verdict, occupant_live_eid = self._classify_rename_collision(occ, src)
            if verdict is None:
                # Occupant is a *different* live entity, or cannot be proven
                # dead: refuse. Nothing is modified (safe no-op), but the user
                # must hear about it — their history is now split across two ids.
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
                return None  # refused: nothing was modified

            merge_reason = verdict
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
                _RENAME_ENTITY_SQL,
                new_entity_id,
                old_entity_id,
            )
        return merge_reason, merged_orphan_rows, dropped_duplicate_rows

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
                                _RENAME_ENTITY_SQL,
                                new_entity_id,
                                old_entity_id,
                            )
                    except asyncpg.UniqueViolationError:
                        outcome = await self._take_over_occupied_name(
                            conn, old_entity_id, new_entity_id
                        )
                        if outcome is None:
                            return
                        (
                            merge_reason,
                            merged_orphan_rows,
                            dropped_duplicate_rows,
                        ) = outcome

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
                    # Bound the query. This service takes arbitrary SQL from the
                    # UI or an automation, and an unbounded one pins a pooled
                    # connection while it works the server — a single careless
                    # aggregate over a large hypertable can starve the writer and
                    # drag the whole machine into swap. SET LOCAL is scoped to
                    # this transaction, so nothing else is affected.
                    await conn.execute(
                        f"SET LOCAL statement_timeout = {QUERY_TIMEOUT_MS}"
                    )
                    rows = await conn.fetch(sql)
                    # Through the same sanitizer the write path uses: a query
                    # can select any type at all, and the result becomes a
                    # service response Home Assistant has to serialize.
                    return [self._sanitize_obj(dict(row)) for row in rows]
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

    # ------------------------------------------------------------------
    # Statistics (each one is optional and independently reported)
    # ------------------------------------------------------------------

    async def _get_states_chunk_stats(self):
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

    async def _get_states_size_stats(self):
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

    async def _get_events_chunk_stats(self):
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

    async def _get_events_size_stats(self):
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

    async def get_db_stats(self, stats_type: str = "all"):
        """Fetch database statistics using TimescaleDB chunks view.

        Args:
            stats_type: Type of stats to fetch - "chunk", "size", or "all"
        """
        stats = {}
        if not self._pool:
            return stats

        tasks = []

        if self.record_states:
            if stats_type in ("chunk", "all"):
                tasks.append(self._get_states_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(self._get_states_size_stats())

        if self.record_events:
            if stats_type in ("chunk", "all"):
                tasks.append(self._get_events_chunk_stats())
            if stats_type in ("size", "all"):
                tasks.append(self._get_events_size_stats())

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
