import logging
import asyncio
from datetime import timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_LOGGER = logging.getLogger(__name__)

async def migrate_database(
    hass, 
    engine: AsyncEngine, 
    record_states: bool, 
    enable_table_entities: bool,
    chunk_time_interval: str = "7 days",
    compress_after: str = "7 days"
):
    # Quickly check if migration is finished to skip the 60s delay
    skip_delay = False
    try:
        async with engine.connect() as conn:
            # If states_raw exists, we skip the initial bootstrap delay.
            # The migration logic below is idempotent and will handle any pending tasks.
            res = await conn.execute(text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states_raw')"))
            if res.scalar():
                skip_delay = True
    except Exception:
        skip_delay = False

    if skip_delay:
        _LOGGER.debug("✅ states_raw detected and migrated, skipping 60s startup delay.")
    else:
        _LOGGER.info("⏳ Migration or initialization needed. Waiting 60 seconds for Home Assistant to complete bootstrap...")
        await asyncio.sleep(60)
    
    _LOGGER.info("🔧 Starting background database migration...")
    
    try:
        if record_states and enable_table_entities:
            # 1. Add constraints FIRST (PK/FK on empty table = instant)
            await _migrate_states_raw_constraints(engine, chunk_time_interval, compress_after)
            
            # 2. Migrate legacy data (chunks, non-blocking)
            await migrate_states_data(engine)
            
            # 3. Convert to hypertable AFTER data (avoid blocking bootstrap)
            await _convert_to_hypertable(engine, chunk_time_interval, compress_after)
        
        await _migrate_events_pk(engine)
        
        _LOGGER.info("🎉 All database migrations completed successfully!")
        
    except Exception as e:
        _LOGGER.error(f"❌ Database migration failed: {e}", exc_info=True)
        _LOGGER.error("   Scribe will continue to work, but some features may be limited.")

async def _migrate_states_raw_constraints(engine: AsyncEngine, chunk_time_interval: str = "7 days", compress_after: str = "7 days"):
    """
    Add PRIMARY KEY and FOREIGN KEY constraints to states_raw.
    Idempotent: if constraints already exist, migration is skipped.
    """
    try:
        async with engine.begin() as conn:
            # Check if PRIMARY KEY already exists
            result = await conn.execute(text("""
                SELECT 1 FROM pg_constraint 
                WHERE conname = 'states_raw_pkey' 
                  AND conrelid = 'states_raw'::regclass
            """))
            
            if result.fetchone():
                _LOGGER.debug("✅ states_raw: PK already exists, skipping")
                return
            
            _LOGGER.info("📊 states_raw: Checking for duplicate rows...")
            
            # Count duplicates
            dup_result = await conn.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT metadata_id, time
                    FROM states_raw
                    GROUP BY metadata_id, time
                    HAVING COUNT(*) > 1
                ) AS duplicates
            """))
            dup_count = dup_result.scalar() or 0
            
            if dup_count > 0:
                _LOGGER.warning(f"⚠️  states_raw: Found {dup_count} duplicate pairs")
                _LOGGER.info("🧹 states_raw: Cleaning duplicates (keeping last row based on ctid)...")
                
                # Delete duplicates, keep the row with the largest ctid
                delete_result = await conn.execute(text("""
                    DELETE FROM states_raw 
                    WHERE ctid NOT IN (
                        SELECT MAX(ctid) 
                        FROM states_raw 
                        GROUP BY metadata_id, time
                    )
                """))
                
                deleted_count = delete_result.rowcount
                _LOGGER.info(f"✅ states_raw: Cleaned {deleted_count} duplicate rows")
            else:
                _LOGGER.debug("✅ states_raw: No duplicates found")
            
            # Add PRIMARY KEY
            _LOGGER.info("🔑 states_raw: Adding PRIMARY KEY (metadata_id, time)...")
            await conn.execute(text("""
                ALTER TABLE states_raw 
                ADD PRIMARY KEY (metadata_id, time)
            """))
            _LOGGER.info("✅ states_raw: PRIMARY KEY added")
            
            # Add FOREIGN KEY
            _LOGGER.info("🔗 states_raw: Adding FOREIGN KEY (metadata_id → entities.id)...")
            await conn.execute(text("""
                ALTER TABLE states_raw 
                ADD CONSTRAINT fk_states_raw_entity 
                FOREIGN KEY (metadata_id) 
                REFERENCES entities(id)
            """))
            _LOGGER.info("✅ states_raw: FOREIGN KEY added")
            
    except Exception as e:
        _LOGGER.error(f"❌ states_raw constraints migration failed: {e}")
        raise e

async def _convert_to_hypertable(engine: AsyncEngine, chunk_time_interval: str = "7 days", compress_after: str = "7 days"):
    """
    Convert states_raw to TimescaleDB hypertable and enable compression.
    This is done AFTER data migration to avoid blocking HA bootstrap.
    """
    try:
        async with engine.begin() as conn:
            # Check if already a hypertable
            hypertable_check = await conn.execute(text("""
                SELECT 1 FROM timescaledb_information.hypertables 
                WHERE hypertable_name = 'states_raw'
            """))
            
            if not hypertable_check.fetchone():
                _LOGGER.info(f"🕐 states_raw: Converting to TimescaleDB hypertable (chunk: {chunk_time_interval})...")
                await conn.execute(text(f"""
                    SELECT create_hypertable('states_raw', 'time', 
                        chunk_time_interval => INTERVAL '{chunk_time_interval}',
                        if_not_exists => TRUE
                    )
                """))
                _LOGGER.info(f"✅ states_raw: Converted to hypertable ({chunk_time_interval} chunks)")
                
                # Add compression policy
                _LOGGER.info(f"🗜️ states_raw: Adding compression policy (compress after {compress_after})...")
                await conn.execute(text("""
                    ALTER TABLE states_raw SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = 'metadata_id'
                    )
                """))
                await conn.execute(text(f"""
                    SELECT add_compression_policy('states_raw', INTERVAL '{compress_after}')
                """))
                _LOGGER.info(f"✅ states_raw: Compression enabled (after {compress_after})")
            else:
                _LOGGER.debug("✅ states_raw: Already a hypertable")
    except Exception as e:
        _LOGGER.warning(f"⚠️ states_raw: Could not convert to hypertable: {e}")
        _LOGGER.info("   (TimescaleDB extension may not be available)")



async def _migrate_events_pk(engine: AsyncEngine):
    """
    Add ID column (PRIMARY KEY) to events table.
    Idempotent: checks if column exists.
    Skips TimescaleDB hypertables (they require time in PK).
    """
    try:
        # Check if events table exists first
        async with engine.connect() as conn:
             table_exists = await conn.execute(text("""
                SELECT 1 FROM information_schema.tables 
                WHERE table_name = 'events'
             """))
             if not table_exists.fetchone():
                 _LOGGER.debug("events table not found (or custom name used), skipping events migration")
                 return
             
             # Check if it's a TimescaleDB hypertable (skip if yes)
             hypertable_check = await conn.execute(text("""
                SELECT 1 FROM timescaledb_information.hypertables 
                WHERE hypertable_name = 'events'
             """))
             if hypertable_check.fetchone():
                 _LOGGER.info("ℹ️ events: Skipping PK migration (TimescaleDB hypertable)")
                 return

        async with engine.begin() as conn:
            # Check if 'id' column exists
            result = await conn.execute(text("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'events' AND column_name = 'id'
            """))
            
            if result.fetchone():
                _LOGGER.debug("✅ events: ID column already exists, skipping")
                return

            _LOGGER.info("🔑 events: Adding 'id' BIGSERIAL PRIMARY KEY column...")
            _LOGGER.info("   (This might take a while on large tables)")
            
            await conn.execute(text("""
                ALTER TABLE events 
                ADD COLUMN id BIGSERIAL PRIMARY KEY
            """))
            
            _LOGGER.info("✅ events: PRIMARY KEY added successfully")

    except Exception as e:
        _LOGGER.error(f"❌ events migration failed: {e}")
        # We don't raise here to avoid blocking other migrations if this one fails

async def migrate_entities_table(conn):
    """Migrate entities table to use SERIAL id as PRIMARY KEY if needed."""
    # Check if 'entities' table exists
    res = await conn.execute(text(
        "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'entities')"
    ))
    table_exists = res.scalar()
    
    if not table_exists:
        _LOGGER.debug("entities table not found, skipping migration (it will be created)")
        return

    # Check if 'id' column exists
    res = await conn.execute(text(
        "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'entities' AND column_name = 'id')"
    ))
    id_exists = res.scalar()
    
    if not id_exists:
        _LOGGER.warning("⚠️ Detected legacy 'entities' table (text PK). Migrating to SERIAL PK...")
        
        # 1. Add 'id' column (auto-populates with sequence)
        await conn.execute(text("ALTER TABLE entities ADD COLUMN id SERIAL"))
        
        # 2. Drop old PK (usually entity_id)
        # Verify constraint name or just drop 'entities_pkey' which is standard
        await conn.execute(text("ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_pkey CASCADE"))
        
        # 3. Add new PK
        await conn.execute(text("ALTER TABLE entities ADD PRIMARY KEY (id)"))
        
        # 4. Add UNIQUE constraint to entity_id (logical key)
        await conn.execute(text("ALTER TABLE entities ADD CONSTRAINT unique_entity_id UNIQUE (entity_id)"))
        
        _LOGGER.info("✅ Entities table migration completed.")

async def migrate_states_data(engine: AsyncEngine):
    """Migrate data from states_legacy to states_raw in chunks."""
    try:
        # Check if 'states_legacy' exists
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'states_legacy')"
            ))
            if not res.scalar():
                return 

        _LOGGER.info("📊 states_legacy: Found legacy data. Starting background migration...")

        # 1. Populate Entities (ensure all legacy entities exist)
        async with engine.begin() as conn:
            _LOGGER.info("Populating entities from legacy states...")
            # Note: We assume entities table has valid schema (id SERIAL) at this point
            await conn.execute(text("""
                INSERT INTO entities (entity_id)
                SELECT DISTINCT entity_id FROM states_legacy
                ON CONFLICT (entity_id) DO NOTHING
            """))

        # 2. Migrate Data (query time range first)
        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT MIN(time), MAX(time) FROM states_legacy"))
            start_time, end_time = res.fetchone()
        
        if start_time is None or end_time is None:
            _LOGGER.info("Legacy table is empty.")
        else:
            _LOGGER.info("Copying state history to states_raw (chunked)...")
            chunk_size = timedelta(hours=12)
            current_time = start_time
            total_duration = (end_time - start_time).total_seconds()
            
            while current_time < end_time:
                next_time = current_time + chunk_size
                
                # Use a fresh transaction per chunk (avoids connection reuse issues)
                async with engine.begin() as conn:
                    await conn.execute(text("""
                        INSERT INTO states_raw (time, metadata_id, state, value, attributes)
                        SELECT 
                            s.time, 
                            e.id, 
                            s.state, 
                            s.value, 
                            s.attributes
                        FROM states_legacy s
                        JOIN entities e ON s.entity_id = e.entity_id
                        WHERE s.time >= :start AND s.time < :end
                        ON CONFLICT (metadata_id, time) DO NOTHING
                    """), {"start": current_time, "end": next_time})
                
                # Progress logging
                elapsed = (current_time - start_time).total_seconds()
                if total_duration > 0:
                    progress = (elapsed / total_duration) * 100
                    _LOGGER.info(f"Migration progress: {progress:.1f}% (current chunk: {current_time})")
                
                current_time = next_time
                await asyncio.sleep(0.1) # Yield to event loop

        # 3. Cleanup
        async with engine.begin() as conn:
            _LOGGER.info("Dropping legacy states table...")
            await conn.execute(text("DROP TABLE states_legacy"))
            
        _LOGGER.info("✅ Data migration completed successfully!")

    except Exception as e:
        _LOGGER.error(f"❌ Data migration failed: {e}")
