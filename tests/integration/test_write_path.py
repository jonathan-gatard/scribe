"""End-to-end write path: flush, metadata resolution, sanitization, buffering.

Several tests here pin regressions that shipped in past releases (#35, #40);
all of them go through the real COPY into a real hypertable, which is where
those bugs actually manifested.
"""

import dataclasses
import math
import uuid
from datetime import date, datetime, timezone

import pytest

from .conftest import BASE_TIME, entity_rows, make_writer, write_event, write_states


@dataclasses.dataclass
class _Channel:
    """Stand-in for the integration objects that crashed json.dumps in #35."""

    name: str
    number: int


class _Opaque:
    """A value with no JSON representation at all."""

    def __str__(self):
        return "opaque-value"


@pytest.mark.asyncio
async def test_states_land_with_values_and_attributes(writer, db):
    """A flushed state round-trips: time, state, numeric value, jsonb attributes."""
    await write_states(
        writer,
        "sensor.temp",
        1,
        state="21.5",
        value=21.5,
        attributes={"unit": "°C", "nested": {"a": [1, 2]}},
    )

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT time, state, value, attributes FROM states "
            "WHERE entity_id = 'sensor.temp'"
        )
    assert row["time"] == BASE_TIME
    assert row["state"] == "21.5"
    assert row["value"] == 21.5
    assert row["attributes"] == {"unit": "°C", "nested": {"a": [1, 2]}}


@pytest.mark.asyncio
async def test_unknown_entity_gets_a_metadata_row(writer, db):
    """States for an entity Scribe has never seen create its `entities` row."""
    await write_states(writer, "sensor.brand_new", 3)

    mid, count = await entity_rows(db, "sensor.brand_new")
    assert mid is not None
    assert count == 3


@pytest.mark.asyncio
async def test_repeated_flushes_reuse_one_metadata_row(writer, db):
    """The entity cache must not create a second row on later batches."""
    await write_states(writer, "sensor.stable", 2, start=0)
    await write_states(writer, "sensor.stable", 2, start=10)

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities WHERE entity_id = 'sensor.stable'"
            )
            == 1
        )
    _, count = await entity_rows(db, "sensor.stable")
    assert count == 4


@pytest.mark.asyncio
async def test_datetime_attributes_survive(writer, db):
    """Regression #40: datetime/date in attributes crashed the whole batch."""
    await write_states(
        writer,
        "sensor.clock",
        1,
        attributes={
            "last_seen": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "day": date(2026, 1, 2),
        },
    )

    async with db.acquire() as conn:
        attrs = await conn.fetchval(
            "SELECT attributes FROM states WHERE entity_id = 'sensor.clock'"
        )
    assert attrs["last_seen"].startswith("2026-01-02")
    assert attrs["day"] == "2026-01-02"


@pytest.mark.asyncio
async def test_non_serializable_attributes_survive(writer, db):
    """Regression #35: a custom object in attributes killed the batch.

    Dataclasses keep their field names; anything else degrades to its string
    form rather than taking the flush down with it.
    """
    await write_states(
        writer,
        "sensor.weird",
        1,
        attributes={
            "channel": _Channel(name="HD1", number=7),
            "opaque": _Opaque(),
            "ident": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        },
    )

    async with db.acquire() as conn:
        attrs = await conn.fetchval(
            "SELECT attributes FROM states WHERE entity_id = 'sensor.weird'"
        )
    assert attrs["channel"] == {"name": "HD1", "number": 7}
    assert attrs["opaque"] == "opaque-value"
    assert attrs["ident"] == "12345678-1234-5678-1234-567812345678"


@pytest.mark.asyncio
async def test_non_finite_values_become_null(writer, db):
    """NaN/Infinity are not valid JSON numbers and must not reach the codec."""
    await write_states(
        writer,
        "sensor.inf",
        1,
        attributes={"nan": math.nan, "inf": math.inf, "ok": 1.5},
    )

    async with db.acquire() as conn:
        attrs = await conn.fetchval(
            "SELECT attributes FROM states WHERE entity_id = 'sensor.inf'"
        )
    assert attrs["nan"] is None
    assert attrs["inf"] is None
    assert attrs["ok"] == 1.5


@pytest.mark.asyncio
async def test_null_bytes_are_stripped(writer, db):
    """Postgres text rejects \\x00; it is removed from states and attributes."""
    await write_states(
        writer, "sensor.nulls", 1, state="ba\0d", attributes={"note": "te\0xt"}
    )

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, attributes FROM states WHERE entity_id = 'sensor.nulls'"
        )
    assert row["state"] == "bad"
    assert row["attributes"]["note"] == "text"


@pytest.mark.asyncio
async def test_deeply_nested_attributes_do_not_recurse_forever(writer, db):
    """Sanitization is depth-capped, so a pathological structure still writes."""
    deep = current = {}
    for _ in range(150):
        current["next"] = {}
        current = current["next"]

    await write_states(writer, "sensor.deep", 1, attributes=deep)

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM states WHERE entity_id = 'sensor.deep'"
            )
            == 1
        )


@pytest.mark.asyncio
async def test_events_land_with_context(writer, db):
    """Events go to their own table with origin and context columns."""
    await write_event(
        writer,
        "call_service",
        event_data={"domain": "light", "service": "turn_on"},
        origin="LOCAL",
        context_id="ctx-1",
        context_user_id="user-1",
        context_parent_id=None,
    )

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM events WHERE event_type = 'call_service'"
        )
    assert row["event_data"] == {"domain": "light", "service": "turn_on"}
    assert row["origin"] == "LOCAL"
    assert row["context_id"] == "ctx-1"
    assert row["context_user_id"] == "user-1"
    assert row["context_parent_id"] is None


@pytest.mark.asyncio
async def test_mixed_batch_splits_states_and_events(writer, db):
    """One flush containing both kinds writes each to its own table."""
    writer._queue.append(
        {
            "type": "state",
            "time": BASE_TIME,
            "entity_id": "sensor.mixed",
            "state": "on",
            "value": None,
            "attributes": {},
        }
    )
    writer._queue.append(
        {
            "type": "event",
            "time": BASE_TIME,
            "event_type": "mixed_event",
            "event_data": {},
            "origin": "LOCAL",
            "context_id": None,
            "context_user_id": None,
            "context_parent_id": None,
        }
    )
    await writer._flush()

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM states WHERE entity_id = 'sensor.mixed'"
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type = 'mixed_event'"
            )
            == 1
        )
    assert writer._states_written >= 1
    assert writer._events_written >= 1


@pytest.mark.asyncio
async def test_queue_is_preserved_when_the_database_is_unreachable(writer):
    """buffer_on_failure=True must requeue the batch instead of dropping it."""
    await writer._pool.close()  # every acquire from now on raises
    writer._queue.append(
        {
            "type": "state",
            "time": BASE_TIME,
            "entity_id": "sensor.buffered",
            "state": "on",
            "value": None,
            "attributes": {},
        }
    )

    await writer._flush()

    assert len(writer._queue) == 1, "item was dropped instead of buffered"


@pytest.mark.asyncio
async def test_queue_is_dropped_when_buffering_is_disabled(hass, clean_db):
    """buffer_on_failure=False trades durability for a bounded queue."""
    w = make_writer(hass, buffer_on_failure=False)
    await w.start()
    try:
        await w._pool.close()
        w._queue.append(
            {
                "type": "state",
                "time": BASE_TIME,
                "entity_id": "sensor.dropped",
                "state": "on",
                "value": None,
                "attributes": {},
            }
        )
        await w._flush()
        assert len(w._queue) == 0
        assert w._dropped_events == 1
    finally:
        w._pool = None  # already closed; keep stop() from touching it
        await w.stop()


@pytest.mark.asyncio
async def test_a_null_byte_in_an_attribute_key_does_not_stall_recording(writer, db):
    """PostgreSQL refuses \\u0000 in a jsonb key exactly as in a value.

    Values were cleaned before reaching the codec; keys were not, so one
    attribute key carrying a null byte failed the COPY, which failed the whole
    batch — and since the batch is re-buffered and retried, it failed forever.
    Same permanent stall as the duplicate timestamps fixed in 3.8.
    """
    writer._queue.append(
        {
            "type": "state",
            "time": BASE_TIME,
            "entity_id": "sensor.hostile_attrs",
            "state": "on",
            "value": 1.0,
            "attributes": {"na\0me": "value", (1, 2): "tuple key"},
        }
    )

    await writer._flush()

    assert len(writer._queue) == 0, "the batch must not be stuck in the buffer"
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attributes FROM states WHERE entity_id = 'sensor.hostile_attrs'"
        )
    assert row is not None
    assert row["attributes"]["name"] == "value"
    assert row["attributes"]["(1, 2)"] == "tuple key"
