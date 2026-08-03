"""Shared fixtures for tests that run against a real TimescaleDB.

These tests drive the real ScribeWriter against a real database and a real
Home Assistant registry, then assert on what actually landed in the tables —
as opposed to the mocked suites, which assert on the SQL that was issued.

Start the database they expect with:

    docker run -d --name scribe-test-db -e POSTGRES_PASSWORD=scribe \
        -e POSTGRES_DB=scribe -p 55432:5432 timescale/timescaledb:latest-pg17

Every test here skips itself when no database answers, so the suite still runs
anywhere. CI provides a service container and fails if they skip.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import asyncpg
import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.scribe.writer import ScribeWriter

# Host must be the literal 127.0.0.1: pytest-homeassistant-custom-component
# installs pytest-socket with only that address allowed.
DSN = os.environ.get(
    "SCRIBE_TEST_DSN", "postgresql://postgres:scribe@127.0.0.1:55432/scribe"
)

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

# Every relation Scribe may create. `states` is a view on a healthy install but
# a table on a pre-migration one, and `states_legacy` only exists mid-migration,
# so the kind is read from the catalog rather than assumed — DROP VIEW on a
# table (and vice versa) is an error, not a no-op.
_SCRIBE_RELATIONS = (
    "states", "states_legacy", "states_raw", "events",
    "entities", "users", "areas", "devices", "integrations",
)

_RELKIND_KEYWORD = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW"}


async def drop_scribe_relations(conn):
    """Remove every Scribe relation from the public schema, whatever its kind."""
    rows = await conn.fetch(
        """
        -- relkind is Postgres' "char" type, which asyncpg hands back as bytes;
        -- cast it so the lookup below can use plain strings.
        SELECT c.relname, c.relkind::text AS relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
        """,
        list(_SCRIBE_RELATIONS),
    )
    for row in rows:
        keyword = _RELKIND_KEYWORD.get(row["relkind"])
        if keyword:
            await conn.execute(
                f'DROP {keyword} IF EXISTS "{row["relname"]}" CASCADE')


async def dsn_reachable() -> bool:
    """True when a database answers at DSN within a short timeout."""
    try:
        conn = await asyncio.wait_for(asyncpg.connect(DSN), timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture(autouse=True)
def mock_create_pool():
    """Override conftest's autouse patch: real asyncpg, minus idle timers.

    asyncpg arms a `call_later` per pooled connection to retire idle ones. It
    outlives the test and trips Home Assistant's lingering-timer check, so
    pooled connections are made to never expire instead.
    """
    real_create_pool = asyncpg.create_pool

    def factory(*args, **kwargs):
        kwargs.setdefault("max_inactive_connection_lifetime", 0)
        return real_create_pool(*args, **kwargs)

    with patch(
        "custom_components.scribe.writer.asyncpg.create_pool", side_effect=factory
    ):
        yield


@pytest.fixture
async def clean_db(socket_enabled):
    """Drop every Scribe object so each test starts from an empty schema.

    `socket_enabled` lifts pytest-socket's network ban; without it every
    connection attempt raises.
    """
    if not await dsn_reachable():
        pytest.skip(f"no TimescaleDB at {DSN}")
    conn = await asyncpg.connect(DSN)
    try:
        await drop_scribe_relations(conn)
    finally:
        await conn.close()
    yield


def make_writer(hass, **overrides):
    """Build a writer with test-friendly defaults; `overrides` replace any of them."""
    kwargs = dict(
        hass=hass,
        db_url=DSN,
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=100,
        flush_interval=3600,  # never fires on its own; tests flush explicitly
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert=None,
        ssl_cert_file=None,
        ssl_key_file=None,
    )
    kwargs.update(overrides)
    return ScribeWriter(**kwargs)


@pytest.fixture
async def writer(hass, clean_db):
    """A fully started writer — its own pool, codecs, schema and hypertables."""
    w = make_writer(hass)
    await w.start()
    assert w._pool is not None, "writer failed to connect"
    yield w
    await w.stop()  # closes the pool and clears it


@pytest.fixture
async def db(writer):
    """The writer's own pool, so queries share its custom jsonb codec."""
    return writer._pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register_entity(hass, entity_id, unique_id, platform=None):
    """Create a real entity registry entry and assert it got the id we wanted."""
    registry = er.async_get(hass)
    domain, object_id = entity_id.split(".", 1)
    entry = registry.async_get_or_create(
        domain=domain,
        platform=platform or domain,
        unique_id=unique_id,
        suggested_object_id=object_id,
    )
    assert entry.entity_id == entity_id, f"registry gave {entry.entity_id}"
    return entry


async def sync_metadata(writer, hass, entity_id):
    """Mirror what __init__.handle_entity_registry_update writes to `entities`."""
    entity = er.async_get(hass).async_get(entity_id)
    await writer.write_entities([{
        "entity_id": entity.entity_id,
        "unique_id": entity.unique_id,
        "platform": entity.platform,
        "domain": entity.domain,
        "name": entity.name or entity.original_name,
        "device_id": entity.device_id,
        "area_id": entity.area_id,
        "capabilities": None,
    }])


async def write_states(writer, entity_id, count, start=0, **overrides):
    """Enqueue and flush `count` states, one per second from BASE_TIME+start.

    `start` separates entities in time; passing the same value to two entities
    makes their rows collide on states_raw's (metadata_id, time) primary key.
    """
    for i in range(count):
        item = {
            "type": "state",
            "time": BASE_TIME + timedelta(seconds=start + i),
            "entity_id": entity_id,
            "state": f"s{i}",
            "value": float(i),
            "attributes": {},
        }
        item.update(overrides)
        writer._queue.append(item)
    await writer._flush()


async def write_event(writer, event_type, **overrides):
    """Enqueue and flush a single event row."""
    item = {
        "type": "event",
        "time": BASE_TIME,
        "event_type": event_type,
        "event_data": {},
        "origin": "LOCAL",
        "context_id": None,
        "context_user_id": None,
        "context_parent_id": None,
    }
    item.update(overrides)
    writer._queue.append(item)
    await writer._flush()


async def entity_rows(pool, entity_id):
    """Return (entities.id, number of states_raw rows) for an entity_id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM entities WHERE entity_id = $1", entity_id)
        if row is None:
            return None, 0
        n = await conn.fetchval(
            "SELECT count(*) FROM states_raw WHERE metadata_id = $1", row["id"])
        return row["id"], n


async def table_exists(pool, name, kind="BASE TABLE"):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = $1 AND table_type = $2)",
            name, kind,
        )
