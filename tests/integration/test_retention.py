"""Configurable retention against a real TimescaleDB (issue #53).

Retention is a background job owned by TimescaleDB, so the only proof that
Scribe configured it is what `timescaledb_information.jobs` says afterwards —
and the only proof that it *works* is running the job and looking at the rows
that survive. Both are done here for real.
"""

from datetime import timedelta

import pytest

from .conftest import BASE_TIME, make_writer, write_states


async def _retention_jobs(pool, table):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT job_id, config ->> 'drop_after' AS drop_after
            FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_retention' AND hypertable_name = $1
            """,
            table,
        )


async def _drop_after(pool, table):
    rows = await _retention_jobs(pool, table)
    return rows[0]["drop_after"] if rows else None


@pytest.mark.asyncio
async def test_no_policy_by_default(writer, db):
    """History is kept forever unless the user asks otherwise."""
    assert await _retention_jobs(db, "states_raw") == []
    assert await _retention_jobs(db, "events") == []


@pytest.mark.asyncio
async def test_policy_created_from_the_configured_interval(hass, clean_db):
    w = make_writer(hass, retention_states="365 days", retention_events="30 days")
    await w.start()
    try:
        assert await _drop_after(w._pool, "states_raw") == "365 days"
        assert await _drop_after(w._pool, "events") == "30 days"
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_policy_follows_the_setting_across_restarts(hass, clean_db):
    """Changing the value updates the job; clearing it removes the job."""
    w = make_writer(hass, retention_states="365 days")
    await w.start()
    first = await _retention_jobs(w._pool, "states_raw")
    await w.stop()

    w = make_writer(hass, retention_states="90 days")
    await w.start()
    changed = await _retention_jobs(w._pool, "states_raw")
    await w.stop()

    assert len(first) == len(changed) == 1
    assert changed[0]["drop_after"] == "90 days"

    w = make_writer(hass, retention_states="")
    await w.start()
    try:
        assert await _retention_jobs(w._pool, "states_raw") == []
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_unchanged_setting_keeps_the_same_job(hass, clean_db):
    """Re-creating the job on every start would postpone its next run forever."""
    w = make_writer(hass, retention_states="1 month")
    await w.start()
    before = await _retention_jobs(w._pool, "states_raw")
    await w.stop()

    w = make_writer(hass, retention_states="1 month")
    await w.start()
    try:
        after = await _retention_jobs(w._pool, "states_raw")
    finally:
        await w.stop()

    assert before[0]["job_id"] == after[0]["job_id"]


@pytest.mark.asyncio
async def test_retention_actually_drops_old_chunks(hass, clean_db):
    """Run the policy for real and check what survives it."""
    w = make_writer(hass, retention_states="30 days", chunk_interval="1 day")
    await w.start()
    try:
        # Chunks well outside the window, and one inside it. BASE_TIME is a
        # couple of weeks in the past, so "recent" sits inside 30 days.
        for i in range(3):
            w._queue.append(
                {
                    "type": "state",
                    "time": BASE_TIME - timedelta(days=400 + i),
                    "entity_id": "sensor.old",
                    "state": f"s{i}",
                    "value": float(i),
                    "attributes": {},
                }
            )
        await w._flush()
        await write_states(w, "sensor.recent", 3)

        async with w._pool.acquire() as conn:
            job_id = await conn.fetchval(
                "SELECT job_id FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_retention' AND hypertable_name = 'states_raw'"
            )
            await conn.execute("CALL run_job($1)", job_id)

            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states WHERE entity_id = 'sensor.old'"
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM states WHERE entity_id = 'sensor.recent'"
                )
                == 3
            )
            # The entity row itself is metadata, not history: it stays.
            assert await conn.fetchval(
                "SELECT count(*) FROM entities WHERE entity_id = 'sensor.old'"
            )
    finally:
        await w.stop()


@pytest.mark.asyncio
async def test_invalid_interval_is_refused_and_reported(hass, clean_db):
    """A value that could reach SQL must not: no policy, and a Repairs issue."""
    from homeassistant.helpers import issue_registry as ir

    w = make_writer(hass, retention_states="30 days'); DROP TABLE states_raw; --")
    await w.start()
    try:
        assert await _retention_jobs(w._pool, "states_raw") == []
        async with w._pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('states_raw') IS NOT NULL")

        issue = ir.async_get(hass).async_get_issue(
            "scribe", "retention_failed_states_raw"
        )
        assert issue is not None
        assert issue.translation_key == "retention_failed"
    finally:
        await w.stop()
