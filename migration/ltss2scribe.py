import os
import contextlib
import sys
import json
import logging
import psycopg2
from psycopg2.extras import execute_batch, RealDictCursor
from datetime import datetime, timedelta
from dotenv import load_dotenv

from preflight import preflight_scribe_schema

# Load environment variables
load_dotenv()

# Configure logging
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.FileHandler("migration_ltss.log"), logging.StreamHandler()],
)


# --- Configuration Loading ---
def get_env_var(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and not val:
        logging.error(f"Environment variable {name} is required.")
        sys.exit(1)
    return val


# LTSS (Source)
LTSS_HOST = get_env_var("LTSS_HOST", "localhost")
LTSS_PORT = get_env_var("LTSS_PORT", "5432")
LTSS_DB = get_env_var("LTSS_DB", "ltss")
LTSS_USER = get_env_var("LTSS_USER", required=True)
LTSS_PASS = get_env_var("LTSS_PASS", required=True)

# Scribe (Destination)
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

try:
    START_TIME = datetime.fromisoformat(START_TIME_STR)
    END_TIME = datetime.fromisoformat(END_TIME_STR)
except ValueError as e:
    logging.error(f"Error parsing date format: {e}")
    sys.exit(1)

CHUNK_SIZE = timedelta(hours=CHUNK_SIZE_HOURS)


def clean_null_bytes(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


metadata_id_cache = {}


def ensure_metadata_id(pg_cur_scribe, entity_id):
    """
    Ensure an entities entry exists for the given entity_id.
    """
    if entity_id in metadata_id_cache:
        return metadata_id_cache[entity_id]
    # Use `ON CONFLICT DO UPDATE` to ensure that the query always returns an id.
    pg_cur_scribe.execute(
        """
        INSERT INTO entities
            (entity_id) VALUES (%s)
        ON CONFLICT (entity_id) DO UPDATE SET entity_id = %s RETURNING id
        """,
        (entity_id, entity_id),
    )
    pg_cur_scribe.connection.commit()

    metadata_id = pg_cur_scribe.fetchone()[0]
    metadata_id_cache[entity_id] = metadata_id
    return metadata_id


def _clean_attributes(attributes) -> str:
    """Serialize LTSS attributes, stripping null bytes from keys and values."""
    if not isinstance(attributes, dict):
        return json.dumps({})
    return json.dumps(
        {
            clean_null_bytes(k): clean_null_bytes(v) if isinstance(v, str) else v
            for k, v in attributes.items()
        }
    )


def _read_chunk(ltss_conn, scribe_cur, current_start, current_end) -> list:
    """Read one time window from LTSS and turn it into states_raw rows."""
    # A new cursor per chunk: one long-running transaction over the whole
    # migration would pin the source database's snapshot for hours.
    with ltss_conn.cursor(cursor_factory=RealDictCursor) as ltss_cur:
        ltss_cur.execute(
            """
                SELECT time, entity_id, state, attributes
                FROM ltss
                WHERE time >= %s AND time < %s
            """,
            (current_start, current_end),
        )
        rows = ltss_cur.fetchall()

    batch = []
    for row in rows:
        entity_id = clean_null_bytes(row["entity_id"])
        pg_state = clean_null_bytes(row["state"])

        # Non-numeric states keep pg_value NULL and live in pg_state.
        pg_value = None
        if pg_state is not None:
            with contextlib.suppress(ValueError):
                pg_value = float(pg_state)

        batch.append(
            (
                row["time"],
                ensure_metadata_id(scribe_cur, entity_id),
                pg_state,
                pg_value,
                _clean_attributes(row["attributes"]),
            )
        )
    return batch


def _insert_rows(scribe_cur, scribe_conn, rows) -> int:
    """Insert a batch, skipping rows the destination already holds."""
    if not rows:
        return 0
    execute_batch(
        scribe_cur,
        """
        INSERT INTO states_raw (time, metadata_id, state, value, attributes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (metadata_id, time) DO NOTHING
    """,
        rows,
    )
    scribe_conn.commit()
    return len(rows)


def migrate():
    # 1. Connect to Scribe (Destination)
    try:
        scribe_conn = psycopg2.connect(
            host=SCRIBE_HOST,
            port=SCRIBE_PORT,
            database=SCRIBE_DB,
            user=SCRIBE_USER,
            password=SCRIBE_PASS,
        )
        scribe_cur = scribe_conn.cursor()
        logging.info("Connected to Scribe (Destination).")
    except Exception as e:
        logging.error(f"Failed to connect to Scribe: {e}")
        return

    preflight_scribe_schema(scribe_cur)

    # 2. Connect to LTSS (Source)
    try:
        ltss_conn = psycopg2.connect(
            host=LTSS_HOST,
            port=LTSS_PORT,
            database=LTSS_DB,
            user=LTSS_USER,
            password=LTSS_PASS,
        )
        # Use simple cursor for queries
        logging.info("Connected to LTSS (Source).")
    except Exception as e:
        logging.error(f"Failed to connect to LTSS: {e}")
        return

    # 3. Cleanup Destination
    if PURGE_DESTINATION:
        logging.info(
            f"Cleaning existing data in Scribe (states_raw) for range {START_TIME} to {END_TIME}..."
        )
        try:
            scribe_cur.execute(
                "DELETE FROM states_raw WHERE time >= %s AND time <= %s",
                (START_TIME, END_TIME),
            )
            scribe_conn.commit()
            logging.info("Cleanup done.")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
            scribe_conn.rollback()
    else:
        logging.info(
            "Skipping cleanup (PURGE_DESTINATION is False). Data will be appended."
        )

    # 4. Chunk Loop
    current_start = START_TIME
    total_migrated_rows = 0

    logging.info(
        f"Starting migration from {START_TIME} to {END_TIME} in chunks of {CHUNK_SIZE_HOURS} hours."
    )

    while current_start < END_TIME:
        current_end = min(current_start + CHUNK_SIZE, END_TIME)
        logging.info(f"--- Processing Chunk: {current_start} to {current_end} ---")

        try:
            rows = _read_chunk(ltss_conn, scribe_cur, current_start, current_end)
            inserted = _insert_rows(scribe_cur, scribe_conn, rows)
            total_migrated_rows += inserted
            if inserted:
                logging.info(f"   -> Imported {inserted} rows.")
            else:
                logging.info("   -> No data in this chunk.")
        except Exception as e:
            logging.error(f"Error processing chunk {current_start}: {e}")
            scribe_conn.rollback()

        current_start = current_end

    logging.info(f"Migration complete. Total rows inserted: {total_migrated_rows}")

    scribe_cur.close()
    scribe_conn.close()
    ltss_conn.close()


if __name__ == "__main__":
    migrate()
