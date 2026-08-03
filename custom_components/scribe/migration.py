import logging
import asyncio
from datetime import timedelta

import asyncpg

_LOGGER = logging.getLogger(__name__)


async def _check_timescaledb(pool: asyncpg.Pool) -> bool:
    """Check if TimescaleDB extension is installed in the database."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
            )
            return row is not None
    except Exception as e:
        _LOGGER.warning(
            "[migration._check_timescaledb] Failed to query pg_extension: %s (%s) — assuming TimescaleDB is NOT installed",
            e, type(e).__name__,
        )
        return False


async def migrate_database(
    hass,
    pool: asyncpg.Pool,
    record_states: bool,
    chunk_time_interval: str = "7 days",
    compress_after: str = "7 days"
):
    # Quickly check if migration is finished to skip the 60s delay
    skip_delay = False
    try:
        async with pool.acquire() as conn:
            skip_delay = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states_raw')"
            )
    except Exception as e:
        _LOGGER.warning(
            "[migration.migrate_database] Pre-check for states_raw failed: %s (%s) — will wait 60s before migrating",
            e, type(e).__name__,
        )
        skip_delay = False

    if skip_delay:
        _LOGGER.debug("[migration.migrate_database] ✅ states_raw detected and migrated, skipping 60s startup delay.")
    else:
        _LOGGER.info("[migration.migrate_database] ⏳ Migration or initialization needed. Waiting 60 seconds for Home Assistant to complete bootstrap...")
        await asyncio.sleep(60)

    if not skip_delay:
        _LOGGER.info("[migration.migrate_database] 🔧 Starting background database migration...")

    # Detect TimescaleDB once for all migration steps
    has_timescaledb = await _check_timescaledb(pool)
    if has_timescaledb:
        _LOGGER.info("[migration.migrate_database] 📦 TimescaleDB extension detected")
    else:
        _LOGGER.info("[migration.migrate_database] ℹ️ TimescaleDB extension not installed — skipping hypertable features")

    performed_any = False
    try:
        if record_states:
            # 1. Add constraints FIRST (PK/FK on empty table = instant)
            if await _migrate_states_raw_constraints(pool, has_timescaledb, chunk_time_interval, compress_after):
                performed_any = True

            # 2. Migrate legacy data (chunks, non-blocking)
            if await migrate_states_data(pool):
                performed_any = True

            # 3. Convert to hypertable AFTER data (avoid blocking bootstrap)
            if has_timescaledb:
                if await _convert_to_hypertable(pool, chunk_time_interval, compress_after):
                    performed_any = True

        if await _migrate_events_pk(pool, has_timescaledb):
            performed_any = True

        if performed_any:
            _LOGGER.info("[migration.migrate_database] 🎉 All database migrations completed successfully!")

    except Exception as e:
        _LOGGER.error(
            "[migration.migrate_database] ❌ Database migration failed: %s (%s). Scribe will continue to work, but some features may be limited.",
            e, type(e).__name__, exc_info=True,
        )


async def _migrate_states_raw_constraints(pool: asyncpg.Pool, has_timescaledb: bool, chunk_time_interval: str = "7 days", compress_after: str = "7 days"):
    """
    Add PRIMARY KEY and FOREIGN KEY constraints to states_raw.
    Idempotent: if constraints already exist, migration is skipped.
    Handles compressed hypertables by temporarily disabling compression.
    """
    step = "init"
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Check if PRIMARY KEY already exists
                step = "check_existing_pk"
                pk_exists = await conn.fetchrow("""
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'states_raw_pkey'
                      AND conrelid = 'states_raw'::regclass
                """)

                if pk_exists:
                    _LOGGER.debug("[migration._migrate_states_raw_constraints] ✅ states_raw: PK already exists, skipping")
                    return False

                # Check if this is a hypertable and disable compression if needed
                is_hypertable = False
                if has_timescaledb:
                    step = "check_hypertable_compression"
                    compression_row = await conn.fetchrow("""
                        SELECT compression_enabled FROM timescaledb_information.hypertables
                        WHERE hypertable_name = 'states_raw'
                    """)
                    if compression_row:
                        is_hypertable = True
                        if compression_row['compression_enabled']:
                            _LOGGER.info("[migration._migrate_states_raw_constraints] 🗜️ states_raw: Temporarily disabling compression for constraint migration...")
                            step = "remove_compression_policy"
                            await conn.execute("SELECT remove_compression_policy('states_raw', if_exists => true)")
                            step = "disable_compression"
                            await conn.execute("ALTER TABLE states_raw SET (timescaledb.compress = false)")

                _LOGGER.info("[migration._migrate_states_raw_constraints] 📊 states_raw: Checking for duplicate rows...")

                # Count duplicates
                step = "count_duplicates"
                dup_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM (
                        SELECT metadata_id, time
                        FROM states_raw
                        GROUP BY metadata_id, time
                        HAVING COUNT(*) > 1
                    ) AS duplicates
                """) or 0

                if dup_count > 0:
                    _LOGGER.warning(
                        "[migration._migrate_states_raw_constraints] ⚠️  states_raw: Found %d duplicate (metadata_id, time) pairs — cleaning up (keeping last row via ctid)...",
                        dup_count,
                    )

                    # asyncpg.execute() returns a status string like "DELETE 5"
                    step = "delete_duplicates"
                    status = await conn.execute("""
                        DELETE FROM states_raw
                        WHERE ctid NOT IN (
                            SELECT MAX(ctid)
                            FROM states_raw
                            GROUP BY metadata_id, time
                        )
                    """)
                    # Parse "DELETE N" to get count
                    deleted_count = int(status.split()[-1]) if status else 0
                    _LOGGER.info("[migration._migrate_states_raw_constraints] ✅ states_raw: Cleaned %d duplicate rows", deleted_count)
                else:
                    _LOGGER.debug("[migration._migrate_states_raw_constraints] ✅ states_raw: No duplicates found")

                # Add PRIMARY KEY
                _LOGGER.info("[migration._migrate_states_raw_constraints] 🔑 states_raw: Adding PRIMARY KEY (metadata_id, time)...")
                step = "add_primary_key"
                await conn.execute("""
                    ALTER TABLE states_raw
                    ADD PRIMARY KEY (metadata_id, time)
                """)
                _LOGGER.info("[migration._migrate_states_raw_constraints] ✅ states_raw: PRIMARY KEY added")

                # Add FOREIGN KEY
                _LOGGER.info("[migration._migrate_states_raw_constraints] 🔗 states_raw: Adding FOREIGN KEY (metadata_id → entities.id)...")
                step = "add_foreign_key"
                await conn.execute("""
                    ALTER TABLE states_raw
                    ADD CONSTRAINT fk_states_raw_entity
                    FOREIGN KEY (metadata_id)
                    REFERENCES entities(id)
                """)
                _LOGGER.debug("[migration._migrate_states_raw_constraints] ✅ states_raw: FOREIGN KEY added")

                # Always enable compression on hypertables after constraints are added
                if is_hypertable:
                    _LOGGER.info("[migration._migrate_states_raw_constraints] 🗜️ states_raw: Re-enabling compression...")
                    step = "reenable_compression"
                    await conn.execute("""
                        ALTER TABLE states_raw SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'metadata_id'
                        )
                    """)
                    step = "readd_compression_policy"
                    await conn.execute(f"SELECT add_compression_policy('states_raw', INTERVAL '{compress_after}')")
                    _LOGGER.debug("[migration._migrate_states_raw_constraints] ✅ states_raw: Compression re-enabled (after %s)", compress_after)

                return True

    except Exception as e:
        _LOGGER.error(
            "[migration._migrate_states_raw_constraints] ❌ states_raw constraints migration failed at step '%s': %s (%s)",
            step, e, type(e).__name__, exc_info=True,
        )
        raise


async def _convert_to_hypertable(pool: asyncpg.Pool, chunk_time_interval: str = "7 days", compress_after: str = "7 days"):
    """
    Convert states_raw to TimescaleDB hypertable and enable compression.
    This is done AFTER data migration to avoid blocking HA bootstrap.
    """
    step = "init"
    try:
        async with pool.acquire() as conn:
            # Check if already a hypertable
            step = "check_is_hypertable"
            already_hypertable = await conn.fetchrow("""
                SELECT 1 FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'states_raw'
            """)

            if not already_hypertable:
                _LOGGER.info(
                    "[migration._convert_to_hypertable] 🕐 states_raw: Converting to TimescaleDB hypertable (chunk: %s)...",
                    chunk_time_interval,
                )
                step = "create_hypertable"
                await conn.execute(f"""
                    SELECT create_hypertable('states_raw', 'time',
                        chunk_time_interval => INTERVAL '{chunk_time_interval}',
                        if_not_exists => TRUE
                    )
                """)
                _LOGGER.debug(
                    "[migration._convert_to_hypertable] ✅ states_raw: Converted to hypertable (%s chunks)",
                    chunk_time_interval,
                )

                # Add compression policy
                _LOGGER.debug(
                    "[migration._convert_to_hypertable] 🗜️ states_raw: Adding compression policy (compress after %s)...",
                    compress_after,
                )
                step = "enable_compression"
                await conn.execute("""
                    ALTER TABLE states_raw SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = 'metadata_id'
                    )
                """)
                step = "add_compression_policy"
                await conn.execute(f"""
                    SELECT add_compression_policy('states_raw', INTERVAL '{compress_after}')
                """)
                _LOGGER.debug(
                    "[migration._convert_to_hypertable] ✅ states_raw: Compression enabled (after %s)",
                    compress_after,
                )
                return True
            else:
                _LOGGER.debug("[migration._convert_to_hypertable] ✅ states_raw: Already a hypertable")
                return False

    except Exception as e:
        _LOGGER.warning(
            "[migration._convert_to_hypertable] ⚠️ states_raw: Hypertable conversion failed at step '%s': %s (%s). TimescaleDB extension may not be available or permissions may be missing.",
            step, e, type(e).__name__, exc_info=True,
        )
        return False


async def _migrate_events_pk(pool: asyncpg.Pool, has_timescaledb: bool):
    """
    Add ID column (PRIMARY KEY) to events table.
    Idempotent: checks if column exists.
    Skips TimescaleDB hypertables (they require time in PK).
    """
    step = "init"
    try:
        async with pool.acquire() as conn:
            # Check if events table exists first
            step = "check_events_table_exists"
            table_exists = await conn.fetchrow("""
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'events'
            """)
            if not table_exists:
                _LOGGER.debug("[migration._migrate_events_pk] events table not found (or custom name used), skipping events migration")
                return False

            # Check if it's a TimescaleDB hypertable (skip if yes)
            if has_timescaledb:
                step = "check_events_hypertable"
                is_hypertable = await conn.fetchrow("""
                    SELECT 1 FROM timescaledb_information.hypertables
                    WHERE hypertable_name = 'events'
                """)
                if is_hypertable:
                    _LOGGER.debug("[migration._migrate_events_pk] ℹ️ events: Skipping PK migration (TimescaleDB hypertable)")
                    return False

            # Check if 'id' column already exists
            step = "check_id_column"
            id_exists = await conn.fetchrow("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'events' AND column_name = 'id'
            """)
            if id_exists:
                _LOGGER.debug("[migration._migrate_events_pk] ✅ events: ID column already exists, skipping")
                return False

            _LOGGER.info("[migration._migrate_events_pk] 🔑 events: Adding 'id' BIGSERIAL PRIMARY KEY column...")
            _LOGGER.info("[migration._migrate_events_pk]   (This might take a while on large tables)")

            step = "add_id_column"
            async with conn.transaction():
                await conn.execute("""
                    ALTER TABLE events
                    ADD COLUMN id BIGSERIAL PRIMARY KEY
                """)

            _LOGGER.debug("[migration._migrate_events_pk] ✅ events: PRIMARY KEY added successfully")
            return True

    except Exception as e:
        _LOGGER.error(
            "[migration._migrate_events_pk] ❌ events migration failed at step '%s': %s (%s)",
            step, e, type(e).__name__, exc_info=True,
        )
        # We don't raise here to avoid blocking other migrations if this one fails
        return False


async def migrate_entities_table(conn):
    """Migrate entities table to use SERIAL id as PRIMARY KEY if needed.

    Receives an asyncpg connection directly (called from within an open transaction
    in writer.py's _init_entities_table).
    """
    try:
        step = "check_entities_table"
        # Check if 'entities' table exists
        table_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'entities')"
        )

        if not table_exists:
            _LOGGER.debug("[migration.migrate_entities_table] entities table not found, skipping migration (it will be created)")
            return

        # Check if 'id' column exists
        step = "check_id_column"
        id_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'entities' AND column_name = 'id')"
        )

        if not id_exists:
            _LOGGER.warning("[migration.migrate_entities_table] ⚠️ Detected legacy 'entities' table (text PK). Migrating to SERIAL PK...")

            # 1. Add 'id' column (auto-populates with sequence)
            step = "add_id_column"
            await conn.execute("ALTER TABLE entities ADD COLUMN id SERIAL")

            # 2. Drop old PK (usually entity_id)
            step = "drop_old_pk"
            await conn.execute("ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_pkey CASCADE")

            # 3. Add new PK
            step = "add_new_pk"
            await conn.execute("ALTER TABLE entities ADD PRIMARY KEY (id)")

            # 4. Add UNIQUE constraint to entity_id (logical key)
            step = "add_unique_entity_id"
            await conn.execute("ALTER TABLE entities ADD CONSTRAINT unique_entity_id UNIQUE (entity_id)")

            _LOGGER.info("[migration.migrate_entities_table] ✅ Entities table migration completed.")
    except Exception as e:
        _LOGGER.error(
            "[migration.migrate_entities_table] ❌ entities migration failed at step '%s': %s (%s)",
            step, e, type(e).__name__, exc_info=True,
        )
        raise


async def migrate_states_data(pool: asyncpg.Pool):
    """Migrate data from states_legacy to states_raw in chunks."""
    try:
        step = "check_states_legacy"
        # Check if 'states_legacy' exists
        async with pool.acquire() as conn:
            legacy_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states_legacy')"
            )
            if not legacy_exists:
                return False

        _LOGGER.info("[migration.migrate_states_data] 📊 states_legacy: Found legacy data. Starting background migration...")

        # 1. Populate Entities (ensure all legacy entities exist)
        step = "populate_entities"
        async with pool.acquire() as conn:
            async with conn.transaction():
                _LOGGER.info("[migration.migrate_states_data] Populating entities from legacy states...")
                await conn.execute("""
                    INSERT INTO entities (entity_id)
                    SELECT DISTINCT entity_id FROM states_legacy
                    ON CONFLICT (entity_id) DO NOTHING
                """)

        # 2. Migrate Data (query time range first)
        step = "query_time_range"
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT MIN(time), MAX(time) FROM states_legacy")
            start_time, end_time = row[0], row[1]

        if start_time is None or end_time is None:
            _LOGGER.info("[migration.migrate_states_data] Legacy table is empty.")
        else:
            _LOGGER.info("[migration.migrate_states_data] Copying state history to states_raw (chunked)...")
            chunk_size = timedelta(hours=12)
            current_time = start_time
            total_duration = (end_time - start_time).total_seconds()

            step = "copy_chunks"
            while current_time < end_time:
                next_time = current_time + chunk_size

                # Use a fresh connection + transaction per chunk
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("""
                            INSERT INTO states_raw (time, metadata_id, state, value, attributes)
                            SELECT
                                s.time,
                                e.id,
                                s.state,
                                s.value,
                                s.attributes
                            FROM states_legacy s
                            JOIN entities e ON s.entity_id = e.entity_id
                            WHERE s.time >= $1 AND s.time < $2
                            ON CONFLICT (metadata_id, time) DO NOTHING
                        """, current_time, next_time)

                # Progress logging
                elapsed = (current_time - start_time).total_seconds()
                if total_duration > 0:
                    progress = (elapsed / total_duration) * 100
                    _LOGGER.info(
                        "[migration.migrate_states_data] Progress: %.1f%% (current chunk: %s → %s)",
                        progress, current_time, next_time,
                    )

                current_time = next_time
                await asyncio.sleep(0.1)  # Yield to event loop

        # 3. Cleanup
        step = "drop_legacy"
        async with pool.acquire() as conn:
            async with conn.transaction():
                _LOGGER.info("[migration.migrate_states_data] Dropping legacy states table...")
                await conn.execute("DROP TABLE states_legacy CASCADE")

        _LOGGER.debug("[migration.migrate_states_data] ✅ Data migration completed successfully!")
        return True

    except Exception as e:
        _LOGGER.error(
            "[migration.migrate_states_data] ❌ Data migration failed at step '%s': %s (%s)",
            step, e, type(e).__name__, exc_info=True,
        )
        return False
