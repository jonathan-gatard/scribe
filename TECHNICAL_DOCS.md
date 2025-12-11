# Scribe Technical Documentation

## Overview
Scribe is a custom Home Assistant integration designed to offload historical data recording to a high-performance **TimescaleDB** (PostgreSQL) database. It runs alongside the default `recorder` but offers superior performance for long-term data storage and analysis thanks to TimescaleDB's hypertables and compression.

## Scribe vs Native Recorder

| Feature | Native Recorder (PostgreSQL) | Scribe (TimescaleDB) |
| :--- | :--- | :--- |
| **Primary Purpose** | Core HA History, Logbook, Energy Dashboard | Long-term Analysis, Grafana, Data Science |
| **Data Retention** | Usually short (e.g., 10-30 days) | Infinite / Long-term (Years) |
| **Storage Engine** | Standard SQL Tables | **Hypertables** (Partitioned by time) |
| **Compression** | None (Row-based) | **Columnar Compression** (90%+ savings) |
| **Performance** | Good for recent history | Excellent for aggregations & large datasets |
| **Integration** | Native UI (Logbook, History) | External Tools (Grafana, pgAdmin) |
| **Configuration** | `recorder:` in YAML | UI Config Flow + Advanced YAML |

**Recommendation**: Run Scribe **in parallel** with the native Recorder.
*   Keep Native Recorder retention short (e.g., 7-14 days) for fast UI performance.
*   Use Scribe for long-term storage (e.g., 1 year+) and visualization in Grafana.
*   Scribe does **not** populate the Home Assistant History, Logbook, or Energy Dashboard.

## Architecture

### Core Components
1.  **`__init__.py`**: The entry point.
    *   Sets up the integration.
    *   Initializes the `ScribeWriter` thread.
    *   Registers event listeners (`EVENT_STATE_CHANGED`, `EVENT_HOMEASSISTANT_STOP`).
    *   Registers event listeners (`EVENT_STATE_CHANGED`, `EVENT_HOMEASSISTANT_STOP`).
    *   Handles the `scribe.flush` and `scribe.query` services.
    *   Forwards setup to `sensor` and `binary_sensor` platforms.
2.  **`writer.py` (`ScribeWriter`)**: The heart of the integration.
    *   Runs as a separate **Daemon Thread** to avoid blocking the main Home Assistant loop.
    *   **Queue System**: Events are added to a thread-safe list (`self._queue`) protected by a `threading.Lock`.
    *   **Batch Processing**: Data is flushed to the database in batches (default: 100 items) or periodically (default: 5 seconds).
    *   **Database Management**: Automatically handles table creation, hypertable conversion, and compression policy application on startup.
    *   **Retry Logic**: If the database is unreachable:
        *   If `buffer_on_failure` is **True**: The batch is put back into the queue (prepended). A `max_queue_size` (default: 10,000) prevents memory exhaustion.
        *   If `buffer_on_failure` is **False** (default): The batch is dropped to prevent memory buildup.
    *   **Query Execution**: Provides a read-only `query()` method for executing SQL SELECT statements safely.
3.  **`config_flow.py`**: Handles UI configuration.
    *   Supports standard user flow and import from YAML.
    *   Validates database connections.
    *   **Auto-Creation**: Can automatically create the target database if it doesn't exist (requires `postgres` user privileges).
4.  **`sensor.py` & `binary_sensor.py`**: Monitoring.
    *   Exposes internal metrics:
        *   `scribe_states_written`: Total state changes written.
        *   `scribe_events_written`: Total events written.
        *   `scribe_states_size_before_compression`: Estimated uncompressed size of states table.
        *   `scribe_states_size_after_compression`: Actual disk size of states table.
        *   `scribe_events_size_before_compression`: Estimated uncompressed size of events table.
        *   `scribe_events_size_after_compression`: Actual disk size of events table.
        *   `scribe_buffer_size`: Current queue size.
        *   `scribe_write_duration`: Time taken for last DB write.
        *   `scribe_states_compression_ratio`: Compression efficiency.
    *   `binary_sensor` indicates connection status.

### Data Flow
1.  **Event Fired**: HA fires an event (e.g., `state_changed`).
2.  **Listener**: `handle_event` in `__init__.py` catches it.
3.  **Filtering**: Checks `include/exclude` domains/entities/globs configuration.
4.  **Enqueue**: The event is processed into a dictionary and passed to `writer.enqueue()`.
5.  **Buffering**: The item is added to `self._queue`.
6.  **Flush Trigger**:
    *   **Size-based**: If queue length >= `batch_size`.
    *   **Time-based**: `run()` loop calls `_flush()` every `flush_interval` seconds.
7.  **Writing**:
    *   `_flush()` acquires the lock.
    *   Swaps the queue with an empty list (atomic-like operation).
    *   Opens a SQLAlchemy connection.
    *   Inserts data using batched `INSERT` for maximum speed.
    *   Commits the transaction.
    *   **On Failure**: The batch is re-queued, and the error is logged.

## Database Schema

Scribe uses two main tables, optimized as TimescaleDB **Hypertables**:

### 1. `states`
Stores numeric and string state data.
*   `time` (TIMESTAMPTZ): Primary partitioning key.
*   `entity_id` (TEXT): The entity ID (e.g., `sensor.temperature`).
*   `state` (TEXT): The raw state string.
*   `value` (DOUBLE PRECISION): Parsed numeric value (for graphing).
*   `attributes` (JSONB): Full state attributes.

**Primary Key Note**: TimescaleDB hypertables are partitioned by time. While we create an index on `(entity_id, time DESC)`, we do not enforce a strict PRIMARY KEY constraint on `time` alone because multiple events can happen at the same microsecond.

**Compression**:
*   Segment by: `entity_id`
*   Order by: `time DESC`
*   Policy: Default 60 days.

### 2. `events`
Stores generic Home Assistant events.
*   `time` (TIMESTAMPTZ): Primary partitioning key.
*   `event_type` (TEXT): e.g., `call_service`, `automation_triggered`.
*   `event_data` (JSONB): The event payload.
*   `origin`, `context_id`, etc.: Traceability data.

**Compression**:
*   Segment by: `event_type`
*   Order by: `time DESC`
*   Policy: Default 60 days.

### 3. `users`
Stores Home Assistant user metadata to provide context for events.
*   `user_id` (TEXT): Primary Key. Matches `context_user_id` in `events` table.
*   `name` (TEXT): User's display name.
*   `is_owner` (BOOLEAN): Whether the user is an owner.
*   `is_active` (BOOLEAN): Whether the user is active.
*   `system_generated` (BOOLEAN): Whether the user is system-generated.
*   `group_ids` (JSONB): List of group IDs the user belongs to.

**Syncing**: This table is automatically synchronized with Home Assistant's user registry on startup.

### `entities` Table

Stores metadata for Home Assistant entities.

| Column | Type | Description |
| :--- | :--- | :--- |
| `entity_id` | TEXT | The entity ID (e.g., `light.living_room`). Primary Key. |
| `unique_id` | TEXT | The unique ID of the entity. |
| `platform` | TEXT | The platform that provides the entity (e.g., `hue`). |
| `domain` | TEXT | The domain of the entity (e.g., `light`). |
| `name` | TEXT | The friendly name of the entity. |
| `device_id` | TEXT | The ID of the device this entity belongs to. |
| `area_id` | TEXT | The ID of the area this entity belongs to. |
| `capabilities` | JSONB | JSON object containing entity capabilities. |
| `last_updated` | TIMESTAMPTZ | When this record was last updated. |

### `areas` Table

Stores metadata for Home Assistant areas.

| Column | Type | Description |
| :--- | :--- | :--- |
| `area_id` | TEXT | The unique ID of the area. Primary Key. |
| `name` | TEXT | The name of the area. |
| `picture` | TEXT | URL or path to the area picture. |
| `last_updated` | TIMESTAMPTZ | When this record was last updated. |

### `devices` Table

Stores metadata for Home Assistant devices.

| Column | Type | Description |
| :--- | :--- | :--- |
| `device_id` | TEXT | The unique ID of the device. Primary Key. |
| `name` | TEXT | The name of the device. |
| `name_by_user` | TEXT | The name assigned by the user. |
| `model` | TEXT | The model of the device. |
| `manufacturer` | TEXT | The manufacturer of the device. |
| `sw_version` | TEXT | The software version of the device. |
| `area_id` | TEXT | The ID of the area this device belongs to. |
| `primary_config_entry` | TEXT | The primary config entry ID for this device. |
| `last_updated` | TIMESTAMPTZ | When this record was last updated. |

### `integrations` Table

Stores metadata for Home Assistant integrations (config entries).

| Column | Type | Description |
| :--- | :--- | :--- |
| `entry_id` | TEXT | The unique ID of the config entry. Primary Key. |
| `domain` | TEXT | The domain of the integration (e.g., `hue`). |
| `title` | TEXT | The title of the integration. |
| `state` | TEXT | The current state of the integration (e.g., `loaded`). |
| `source` | TEXT | The source of the config entry (e.g., `discovery`). |
| `last_updated` | TIMESTAMPTZ | When this record was last updated. |

**Syncing**: This table is automatically synchronized with Home Assistant's entity registry on startup.

## Migration from Recorder

Scribe does not automatically import data from the native Recorder database. However, you can migrate data manually using SQL if both databases are accessible.

**Example Migration Query (PostgreSQL to Scribe)**:
```sql
INSERT INTO scribe_states (time, entity_id, state, value, attributes)
SELECT
    to_timestamp(last_updated_ts),
    entity_id,
    state,
    CASE WHEN state ~ '^[0-9\.]+$' THEN state::DOUBLE PRECISION ELSE NULL END,
    attributes::jsonb
FROM states
WHERE to_timestamp(last_updated_ts) < NOW() - INTERVAL '1 hour';
```
*Note: This is a simplified example. You would need to join with `states_meta` in the native schema to get `entity_id` strings.*

## Error Handling & Reliability

*   **Connection Failures**: If TimescaleDB is down, Scribe will:
    1.  Log an error.
    2.  Mark the `binary_sensor.scribe_database_connection` as `off`.
    3.  **Buffer Data**: Events are kept in memory in the queue.
    4.  **Retry**: The writer will attempt to reconnect and flush the buffer on the next interval.
    5.  **Safety Valve**: If the queue exceeds `max_queue_size` (10,000), new events are dropped to prevent Home Assistant from crashing due to OOM (Out of Memory).

## Troubleshooting

### Common Issues

1.  **"Database connection failed"**:
    *   Check `db_host`, `db_port`, `db_user`, and `db_password`.
    *   Ensure the PostgreSQL server allows connections from the Home Assistant IP (`pg_hba.conf`).
    *   Verify the database exists.

2.  **"Extension timescaledb does not exist"**:
    *   Ensure TimescaleDB is installed on your PostgreSQL server.
    *   Run `CREATE EXTENSION IF NOT EXISTS timescaledb;` in the database.

3.  **High Memory Usage**:
    *   If the DB is down for a long time, the buffer will grow.
    *   Adjust `max_queue_size` in YAML if needed.
    *   Check `sensor.scribe_buffer_size`.

4.  **Missing Data in Grafana**:
    *   Check `sensor.scribe_events_written` to see if data is being written.
    *   Verify your Grafana query uses the correct table name (`states` or custom name).
    *   Check if data is compressed (older data might need `decompress_chunk` for updates, but reads are transparent).

## Configuration

Scribe supports both **YAML** and **UI (Config Flow)** configuration.

*   **YAML**: Best for static settings (Table names, Chunk intervals).
*   **UI**: Best for connection details and filtering options.
*   **Options Flow**: Allows changing filtering and statistics settings without restarting HA.

## Testing & CI

### Unit Tests (`tests/`)
We use `pytest` with `pytest-homeassistant-custom-component`.
*   **`test_config_flow.py`**: Mocks the config flow, verifying inputs and validation logic.
*   **`test_init.py`**: Tests component setup, ensuring the writer starts and stops correctly.
*   **`test_writer.py`**: Tests the `ScribeWriter` class in isolation, mocking the SQLAlchemy engine to verify queueing and flushing logic without a real DB.

### Continuous Integration (GitHub Actions)
Located in `.github/workflows/tests.yaml`.
*   Runs on every `push` and `pull_request`.
*   **Matrix**: Tests against Python 3.12, 3.13, and 3.14.
*   **Steps**:
    1.  Checkout code.
    2.  Install dependencies (`pytest`, `sqlalchemy`, `psycopg2-binary`).
    3.  **Setup Environment**: Creates the `custom_components/scribe` directory structure required for tests to import the component correctly.
    4.  Run `pytest`.

## Deployment

### `scripts/deploy.sh`
A helper script for local development.
1.  **Sync**: Copies `custom_components/scribe` to the Home Assistant `custom_components` directory.
2.  **Clean**: Removes unnecessary dev files (tests, cache) from the target.
3.  **Restart**: Restarts the Home Assistant container to apply changes.

---
*Generated by Antigravity*
