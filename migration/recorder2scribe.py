"""
Migrate data from a Home Assistant recorder database stored in PostgreSQL or SQLite to scribe.

Currently, only the `states` table is migrated. Duplicate state entries (e.g.,
entries whose timestamp and entity_id are identical) are skipped. This enables
live migration as any entries that were already written by the `scribe` component
will be skipped.

See `.env.example` for an example configuration of the required environment
variables.
"""

import os
import sys
import logging
import sqlite3
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from preflight import preflight_scribe_schema

MINIMUM_RECORDER_SCHEMA_VERSION = 53
"""
Minimum version of the recorder DB schema
"""

# Load environment variables from .env file
load_dotenv()

# Configure logging
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("migration_recorder.log"),
        logging.StreamHandler()
    ]
)

# --- Configuration Loading ---
def get_env_var(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and not val:
        logging.error(f"Environment variable {name} is required.")
        sys.exit(1)
    return val

# Recorder
RECORDER_TYPE = get_env_var("RECORDER_TYPE", "sqlite")
if RECORDER_TYPE == "sqlite":
    RECORDER_DB_PATH = get_env_var("RECORDER_DB_PATH", required=True)
elif RECORDER_TYPE == "postgres":
    RECORDER_HOST = get_env_var("RECORDER_HOST", "localhost")
    RECORDER_PORT = get_env_var("RECORDER_PORT", "5432")
    RECORDER_DB = get_env_var("RECORDER_DB", "homeassistant")
    RECORDER_USER = get_env_var("RECORDER_USER", "homeassistant")
    RECORDER_PASS = get_env_var("RECORDER_PASS", required=True)
else:
    logging.error("RECORDER_TYPE must be 'sqlite' or 'postgres'")
    sys.exit(1)

# Scribe (Postgres)
SCRIBE_HOST = get_env_var("SCRIBE_HOST", "localhost")
SCRIBE_PORT = get_env_var("SCRIBE_PORT", "5432")
SCRIBE_DB = get_env_var("SCRIBE_DB", "scribe")
SCRIBE_USER = get_env_var("SCRIBE_USER", "scribe")
SCRIBE_PASS = get_env_var("SCRIBE_PASS", required=True)

# Migration Settings
START_TIME_STR = get_env_var("MIGRATION_START_TIME", required=True)
END_TIME_STR = get_env_var("MIGRATION_END_TIME", required=True)
PURGE_DESTINATION = get_env_var("PURGE_DESTINATION", "False").lower() == "true"
CHUNK_SIZE_HOURS = int(get_env_var("CHUNK_SIZE", "4"))

metadata_id_cache = {}

try:
    START_TIME = datetime.fromisoformat(START_TIME_STR)
    END_TIME = datetime.fromisoformat(END_TIME_STR)
except ValueError as e:
    logging.error(f"Error parsing date format: {e}")
    sys.exit(1)

CHUNK_SIZE = timedelta(hours=CHUNK_SIZE_HOURS)


def clean_null_bytes(value):
    """
    Remove NUL (0x00) bytes from strings to prevent Postgres errors.
    """
    if isinstance(value, str):
        return value.replace('\x00', '')
    return value

def ensure_metadata_id(pg_cur_scribe, entity_id):
    """
    Ensure a entities entry exists for the given entity_id.
    """
    if entity_id in metadata_id_cache:
        return metadata_id_cache[entity_id]
    # Use `ON CONFLICT DO UPDATE` to ensure that the query always returns an id.
    pg_cur_scribe.execute("""
        INSERT INTO entities
            (entity_id) VALUES (%s)
        ON CONFLICT (entity_id) DO UPDATE SET entity_id = %s RETURNING id
        """, (entity_id, entity_id))
    pg_cur_scribe.connection.commit()

    metadata_id = pg_cur_scribe.fetchone()[0]
    metadata_id_cache[entity_id] = metadata_id
    return metadata_id

def migrate():
    """
    Main migration logic.
    """
    # 1. Connect to Postgres (Scribe)
    try:
        pg_conn_scribe = psycopg2.connect(
            host=SCRIBE_HOST, port=SCRIBE_PORT, database=SCRIBE_DB, user=SCRIBE_USER, password=SCRIBE_PASS
        )
        pg_cur_scribe = pg_conn_scribe.cursor()
        logging.info("Connected to Scribe (PostgreSQL).")
    except Exception as e:
        sys.exit(f"Failed to connect to Scribe Postgres: {e}")

    preflight_scribe_schema(pg_cur_scribe)

    # 2. Connect to Recorder
    if RECORDER_TYPE == "postgres":
        try:
            conn_recorder = psycopg2.connect(
                host=RECORDER_HOST, port=RECORDER_PORT, database=RECORDER_DB, user=RECORDER_USER, password=RECORDER_PASS
            )
            cur_recorder = conn_recorder.cursor()
            logging.info("Connected to Recorder (PostgreSQL).")
        except Exception as e:
            sys.exit(f"Failed to connect to Recorder Postgres: {e}")
        placeholder = "%s"
    else:
        try:
            conn_recorder = sqlite3.connect(RECORDER_DB_PATH)
            cur_recorder = conn_recorder.cursor()
            logging.info("Connected to Recorder (SQLite).")
        except Exception as e:
            sys.exit(f"Failed to connect to Recorder SQLite: {e}")
        placeholder = "?"

    cur_recorder.execute("SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1")
    result = cur_recorder.fetchone()
    if result is None or result[0] < MINIMUM_RECORDER_SCHEMA_VERSION:
        sys.exit(f"Schema version {result[0]} is less than minimum supported schema version {MINIMUM_RECORDER_SCHEMA_VERSION}."
                  " Wait for the recorder database migration to finish before reattempting conversion.")

    logging.info(f"Recorder schema version: {result[0]}")


    # 3. Cleanup Destination (Optional)
    if PURGE_DESTINATION:
        logging.info(f"Cleaning existing data in Scribe for range {START_TIME} to {END_TIME}...")
        try:
            pg_cur_scribe.execute("DELETE FROM states_raw WHERE time >= %s AND time < %s", (START_TIME, END_TIME))
            pg_conn_scribe.commit()
            logging.info("Cleanup done.")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
            pg_conn_scribe.rollback()
            sys.exit(1)
    else:
        logging.info("Skipping cleanup (PURGE_DESTINATION is False). Data will be appended.")

    # 4. Chunk Loop
    current_start = START_TIME
    total_migrated_rows = 0

    logging.info(f"Starting migration from {START_TIME} to {END_TIME} in chunks of {CHUNK_SIZE_HOURS} hours.")

    while current_start < END_TIME:
        current_end = current_start + CHUNK_SIZE
        if current_end > END_TIME:
            current_end = END_TIME

        logging.info(f"--- Processing Chunk: {current_start} to {current_end} ---")

        batch = []
        chunk_inserted = 0

        try:
            # Query Recorder
            query = f"""
                SELECT m.entity_id, s.state, s.last_updated_ts, a.shared_attrs
                    FROM states AS s
                    JOIN states_meta AS m ON s.metadata_id = m.metadata_id
                    LEFT JOIN state_attributes AS a ON a.attributes_id = s.attributes_id
                WHERE last_updated_ts >= {placeholder} AND last_updated_ts < {placeholder}
            """
            cur_recorder.execute(query, (current_start.timestamp(), current_end.timestamp()))

            for row in cur_recorder:
                pg_metadata_id = ensure_metadata_id(pg_cur_scribe, clean_null_bytes(row[0]))
                val = row[1]
                ts = datetime.fromtimestamp(row[2], tz=timezone.utc)
                attributes = row[3]

                pg_value = None
                pg_state = None

                if val is not None:
                    # Try to parse value as float, otherwise treat it like a string
                    try:
                        pg_value = float(val)
                    except ValueError:
                        pg_state = clean_null_bytes(val)

                batch.append((ts, pg_metadata_id, pg_state, pg_value, attributes))

            # Insert Batch into Postgres
            if batch:
                inserted = execute_values(pg_cur_scribe, """
                    INSERT INTO states_raw (time, metadata_id, state, value, attributes)
                    VALUES %s ON CONFLICT DO NOTHING
                    RETURNING (time, metadata_id)
                """, batch, fetch=True)
                pg_conn_scribe.commit()
                chunk_inserted = len(inserted)
                if len(batch) != chunk_inserted:
                    logging.warning(f"   -> {len(batch) - chunk_inserted} rows were skipped due to conflicts.")
                total_migrated_rows += chunk_inserted

            logging.info(f"   -> Imported {chunk_inserted} rows.")

        except Exception as e:
            logging.error(f"Error processing chunk {current_start}: {e}", exc_info=True)
            pg_conn_scribe.rollback()
            # We continue to the next chunk even if one fails

        current_start = current_end

    logging.info(f"Migration complete. Total rows inserted: {total_migrated_rows}")

    pg_cur_scribe.close()
    pg_conn_scribe.close()
    cur_recorder.close()
    conn_recorder.close()

if __name__ == "__main__":
    migrate()
