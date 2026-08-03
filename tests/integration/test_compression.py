"""Behaviour against *compressed* TimescaleDB chunks.

Compression is the part of Scribe's storage that only shows its teeth on a
real installation: compressed chunks are read-mostly, and operations that are
trivial on a fresh database (UPDATE, DELETE) either fail or force a
decompression. Every test here compresses a chunk for real before acting on it.
"""

from datetime import timedelta

import pytest

from .conftest import (
    BASE_TIME,
    entity_rows,
    register_entity,
    sync_metadata,
    write_states,
)


async def _compress_all_states_chunks(pool):
    """Compress every states_raw chunk and return how many were compressed."""
    async with pool.acquire() as conn:
        chunks = await conn.fetch(
            "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'states_raw' AND NOT is_compressed"
        )
        for c in chunks:
            await conn.execute(
                f"SELECT compress_chunk('{c['chunk_schema']}.{c['chunk_name']}')"
            )
        return len(chunks)


async def _compressed_chunk_count(pool):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'states_raw' AND is_compressed"
        )


@pytest.mark.asyncio
async def test_chunks_can_be_compressed_and_still_read(writer, db):
    """Compression is configured correctly enough that a chunk actually compresses."""
    await write_states(writer, "sensor.compressed", 20)

    assert await _compress_all_states_chunks(db) >= 1
    assert await _compressed_chunk_count(db) >= 1

    # Reading through the view still returns every row.
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM states WHERE entity_id = 'sensor.compressed'"
            )
            == 20
        )


@pytest.mark.asyncio
async def test_stats_report_compressed_chunks(writer, db):
    """The compression sensors reflect reality once chunks are compressed."""
    await write_states(writer, "sensor.compressed", 20)
    await _compress_all_states_chunks(db)

    stats = await writer.get_db_stats("all")

    assert stats["states_compressed_chunks"] >= 1
    assert stats["states_compressed_size"] > 0
    # The before/after pair the ratio sensor divides must both be populated;
    # they come from hypertable_compression_stats('states_raw') — the
    # hypertable, not the `states` view, which reports nothing.
    assert stats["states_before_compression_total_bytes"] > 0
    assert stats["states_after_compression_total_bytes"] > 0
    assert (
        stats["states_before_compression_total_bytes"]
        > stats["states_after_compression_total_bytes"]
    ), "compression saved nothing"


@pytest.mark.asyncio
async def test_new_states_still_write_after_compression(writer, db):
    """Recent data must keep flowing into the uncompressed head chunk."""
    await write_states(writer, "sensor.ongoing", 5, start=0)
    await _compress_all_states_chunks(db)

    # Far enough ahead to land in a new chunk (chunk_interval is 7 days).
    await write_states(writer, "sensor.ongoing", 5, start=60 * 60 * 24 * 30)

    _, count = await entity_rows(db, "sensor.ongoing")
    assert count == 10


@pytest.mark.asyncio
async def test_rename_merges_history_across_compressed_chunks(writer, hass, db):
    """The rename merge must survive compressed chunks, or lose the rename.

    This is the scenario a long-running installation actually hits: the dead
    orphan's history is old, therefore compressed, and the merge has to
    rewrite its metadata_id. TimescaleDB refuses plain UPDATEs on compressed
    chunks on older versions — if that happens the whole rename transaction
    rolls back and the user silently keeps a split history.
    """
    await register_entity(hass, "sensor.victim", "uid-victim")
    await sync_metadata(writer, hass, "sensor.victim")
    await write_states(writer, "sensor.victim", 10, start=0)
    victim_id, victim_rows = await entity_rows(db, "sensor.victim")
    assert victim_rows == 10

    compressed = await _compress_all_states_chunks(db)
    assert compressed >= 1, "test would be vacuous without a compressed chunk"

    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_remove("sensor.victim")

    await register_entity(hass, "sensor.phoenix", "uid-phoenix")
    await sync_metadata(writer, hass, "sensor.phoenix")
    await write_states(writer, "sensor.phoenix", 3, start=60 * 60 * 24 * 30)

    await writer.rename_entity("sensor.phoenix", "sensor.victim")

    final_id, final_rows = await entity_rows(db, "sensor.victim")
    assert final_id is not None
    assert final_rows == 13, "compressed history was not merged"
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entities WHERE id = $1", victim_id
            )
            == 0
        )


@pytest.mark.asyncio
async def test_duplicate_timestamp_cleanup_works_on_compressed_chunks(writer, hass, db):
    """The pre-merge DELETE also has to reach into compressed chunks."""
    await register_entity(hass, "sensor.victim", "uid-victim")
    await sync_metadata(writer, hass, "sensor.victim")
    await write_states(writer, "sensor.victim", 6, start=0)
    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_remove("sensor.victim")

    await register_entity(hass, "sensor.phoenix", "uid-phoenix")
    await sync_metadata(writer, hass, "sensor.phoenix")
    # Full timestamp overlap with the victim.
    await write_states(writer, "sensor.phoenix", 4, start=0)

    await _compress_all_states_chunks(db)

    await writer.rename_entity("sensor.phoenix", "sensor.victim")

    _, final_rows = await entity_rows(db, "sensor.victim")
    # 4 survivor rows + the victim's 2 non-overlapping ones.
    assert final_rows == 6


@pytest.mark.asyncio
async def test_compression_policy_is_registered(writer, db):
    """A policy must exist, otherwise nothing ever compresses on its own."""
    async with db.acquire() as conn:
        jobs = await conn.fetch(
            "SELECT hypertable_name, config FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression'"
        )
    tables = {j["hypertable_name"] for j in jobs}
    assert "states_raw" in tables
    assert "events" in tables


@pytest.mark.asyncio
async def test_states_view_reads_mixed_compressed_and_live_chunks(writer, db):
    """A query spanning both compressed and uncompressed chunks is complete."""
    await write_states(writer, "sensor.mixed_chunks", 5, start=0)
    await _compress_all_states_chunks(db)
    await write_states(writer, "sensor.mixed_chunks", 5, start=60 * 60 * 24 * 30)

    rows = await writer.query(
        "SELECT time, state FROM states WHERE entity_id = 'sensor.mixed_chunks' "
        "ORDER BY time"
    )

    assert len(rows) == 10
    assert rows[0]["time"] == BASE_TIME
    assert rows[-1]["time"] == BASE_TIME + timedelta(seconds=60 * 60 * 24 * 30 + 4)
