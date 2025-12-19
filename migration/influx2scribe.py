
import os
import sys
import json
import logging
import psycopg2
from psycopg2.extras import execute_batch
from influxdb_client import InfluxDBClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("migration_influx.log"),
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

# InfluxDB
INFLUX_URL = get_env_var("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = get_env_var("INFLUX_TOKEN", required=True)
INFLUX_ORG = get_env_var("INFLUX_ORG", required=True)
INFLUX_BUCKET = get_env_var("INFLUX_BUCKET", required=True)

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

def migrate():
    """
    Main migration logic.
    """
    # 1. Connect to Postgres
    try:
        pg_conn = psycopg2.connect(
            host=SCRIBE_HOST, port=SCRIBE_PORT, database=SCRIBE_DB, user=SCRIBE_USER, password=SCRIBE_PASS
        )
        pg_cur = pg_conn.cursor()
        logging.info("Connected to Scribe (PostgreSQL).")
    except Exception as e:
        logging.error(f"Failed to connect to Postgres: {e}")
        return

    # 2. Connect to InfluxDB
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=300000)
    query_api = client.query_api()
    logging.info("Connected to InfluxDB.")

    # 3. Cleanup Destination (Optional)
    if PURGE_DESTINATION:
        logging.info(f"Cleaning existing data in Scribe for range {START_TIME} to {END_TIME}...")
        try:
            pg_cur.execute(f"DELETE FROM states WHERE time >= '{START_TIME}' AND time <= '{END_TIME}'")
            pg_conn.commit()
            logging.info("Cleanup done.")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
            pg_conn.rollback()
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
        
        # Flux Query to get data pivoted
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {current_start.isoformat()}, stop: {current_end.isoformat()})
          |> filter(fn: (r) => r["_field"] == "value" or r["_field"] == "state")
          |> drop(columns: ["_start", "_stop"])
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        
        batch = []
        chunk_inserted = 0
        
        try:
            # Query InfluxDB
            tables = query_api.query(query)
            
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    entity_id_raw = record.values.get("entity_id")
                    domain = record.values.get("domain")
                    
                    if not entity_id_raw:
                        continue
                    
                    # Fix Entity ID (ensure domain prefix)
                    if domain and not entity_id_raw.startswith(f"{domain}."):
                        pg_entity_id = f"{domain}.{entity_id_raw}"
                    else:
                        pg_entity_id = entity_id_raw
                    
                    pg_entity_id = clean_null_bytes(pg_entity_id)

                    val = record.values.get("value")
                    state_val = record.values.get("state")
                    
                    pg_value = None
                    pg_state = None
                    
                    # Try to parse value as float
                    if val is not None:
                        try:
                            pg_value = float(val)
                        except ValueError:
                            pass
                    
                    # Determine state string
                    if state_val is not None:
                        pg_state = clean_null_bytes(str(state_val))
                    elif pg_value is not None:
                        pg_state = str(pg_value)
                        
                    # Attributes JSON
                    attributes = {}
                    unit = record.values.get("_measurement")
                    if unit and unit != "state":
                         attributes["unit_of_measurement"] = unit
                    
                    for k, v in record.values.items():
                        # Exclude standard fields from attributes
                        if k not in ["result", "table", "_start", "_stop", "_time", "entity_id", "value", "state"]:
                             if v is not None:
                                # Clean keys and values
                                k_clean = clean_null_bytes(k)
                                if isinstance(v, str):
                                    v_clean = clean_null_bytes(v)
                                else:
                                    v_clean = v
                                attributes[k_clean] = v_clean
                    
                    row = (ts, pg_entity_id, pg_state, pg_value, json.dumps(attributes))
                    batch.append(row)
            
            # Insert Batch into Postgres
            if batch:
                execute_batch(pg_cur, """
                    INSERT INTO states (time, entity_id, state, value, attributes)
                    VALUES (%s, %s, %s, %s, %s)
                """, batch)
                pg_conn.commit()
                chunk_inserted = len(batch)
                total_migrated_rows += chunk_inserted
            
            logging.info(f"   -> Imported {chunk_inserted} rows.")
            
        except Exception as e:
            logging.error(f"Error processing chunk {current_start}: {e}")
            pg_conn.rollback()
            # We continue to the next chunk even if one fails
        
        current_start = current_end

    logging.info(f"Migration complete. Total rows inserted: {total_migrated_rows}")
    
    pg_cur.close()
    pg_conn.close()
    client.close()

if __name__ == "__main__":
    migrate()
