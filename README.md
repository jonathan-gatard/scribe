# Scribe

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/jonathan-gatard/scribe?style=for-the-badge)](https://github.com/jonathan-gatard/scribe/releases)
[![License](https://img.shields.io/github/license/jonathan-gatard/scribe?style=for-the-badge)](LICENSE)

**Scribe** is a high-performance custom component for Home Assistant that records your history and long-term statistics directly into **TimescaleDB** (PostgreSQL).

It is designed as a lightweight, "set-and-forget" alternative to the built-in Recorder, optimized for long-term data retention and analysis in Grafana.

## 🚀 Features

- **Performance**: Uses `SQLAlchemy` and batch inserts for minimal impact on Home Assistant.
- **Efficient Storage**: Automatically uses **TimescaleDB Hypertables** and **Native Compression** (up to 95% storage savings).
- **Flexible Schema**: Uses `JSONB` for attributes, allowing for a flexible schema that is still fully queryable.
- **Configurable**: Choose to record **States**, **Events**, or both. Filter entities and domains via the UI.
- **Reliable**: Built-in buffer and retry logic ensures no data loss if the database is temporarily unreachable.
- **UI Configuration**: Fully managed via Home Assistant's Integrations page.

## ⚖️ Scribe vs Native Recorder

| Feature | Native Recorder (PostgreSQL) | Scribe (TimescaleDB) |
| :--- | :--- | :--- |
| **Primary Purpose** | Core HA History, Logbook, Energy Dashboard | Long-term Analysis, Grafana, Data Science |
| **Data Retention** | Usually short (e.g., 10-30 days) | Infinite / Long-term (Years) |
| **Storage Engine** | Standard SQL Tables | **Hypertables** (Partitioned by time) |
| **Compression** | None (Row-based) | **Columnar Compression** (90%+ savings) |
| **Performance** | Good for recent history | Excellent for aggregations & large datasets |
| **Integration** | Native UI (Logbook, History) | External Tools (pgAdmin, etc.) |

**Recommendation**: Run Scribe **in parallel** with the native Recorder. Keep the native recorder retention short (e.g., 7 days) for fast UI performance, and use Scribe for long-term storage.

## 📦 Installation

### Option 1: HACS (Recommended)
1.  Open HACS.
2.  Go to "Integrations" > "Custom repositories".
3.  Add `https://github.com/jonathan-gatard/scribe` as an Integration.
4.  Click "Download".
5.  Restart Home Assistant.

### Option 2: Manual
1.  Copy the `custom_components/scribe` folder to your Home Assistant `config/custom_components/` directory.
2.  Restart Home Assistant.

## ⚙️ Configuration

### Basic Configuration (UI)
1.  Go to **Settings > Devices & Services**.
2.  Click **Add Integration**.
3.  Search for **Scribe**.
4.  Enter your PostgreSQL / TimescaleDB connection details:
    - **Database URL**: `postgresql://scribe:password@host:5432/scribe`
    - **Chunk Interval**: `7 days` (default)
    - **Compress After**: `60 days` (default)
    - **Record States**: Enable to record sensor history (default: True).
    - **Record Events**: Enable to record automation triggers, service calls, etc. (default: False).
    - **Buffer on Failure**: Enable to buffer events when DB is down (default: False).
    - **Max Queue Size**: Max events to buffer (default: 10000).

### Advanced Configuration (YAML Only)
Some advanced settings are only available via `configuration.yaml`:

scribe:
  # Database Connection
  db_url: postgresql://scribe:password@host:5432/scribe

  # Data Retention & Compression
  chunk_time_interval: 7 days  # Time range for each chunk (default: 7 days)
  compress_after: 60 days      # Compress chunks older than this (default: 60 days)

  # Recording Options
  record_states: true          # Record sensor history (default: true)
  record_events: false         # Record automation triggers, etc. (default: false)
  enable_statistics: false     # Enable internal statistics sensors (default: false)

  # Performance & Reliability
  batch_size: 100              # Number of events to buffer before writing (default: 100)
  flush_interval: 5            # Seconds to wait before flushing buffer (default: 5)
  max_queue_size: 10000        # Max events to buffer if DB is down (default: 10000)
  buffer_on_failure: false     # Buffer events if DB is down (default: false)
  debug: false                 # Enable debug logging (default: false)

  # Custom Table Names
  table_name_states: states    # Custom table name for states (default: states)
  table_name_events: events    # Custom table name for events (default: events)

  # Filtering (Include/Exclude)
  # Logic: If include is set, only included items are recorded. Exclude takes precedence over include.
  include_domains:
    - sensor
    - switch
  include_entities:
    - sensor.important_sensor
  exclude_domains:
    - media_player
  exclude_entities:
    - sensor.noisy_sensor
  exclude_attributes:
    - friendly_name
    - icon
    - entity_picture
```

### Database Management Scripts
The `scripts/` directory contains helper scripts for managing the database:
- `setup_scribe_db.sh`: Creates the `scribe` database and user.
- `drop_ha_db.sh`: Drops the `homeassistant` database (use with caution!).

**Note**: If you configure Scribe via YAML, the settings will be imported into the UI config entry. You can still modify them later via the UI "Configure" button.

## 🛠 Services

### `scribe.query`
Execute read-only SQL queries against the TimescaleDB database directly from Home Assistant.

**Example:**
```yaml
service: scribe.query
data:
  sql: "SELECT count(*) FROM states WHERE time > NOW() - INTERVAL '1 day'"
```
Returns: `{"result": [{"count": 12345}]}`

## 📊 Database Schema

Scribe creates two tables (if enabled): `states` and `events`.

### `states` Table
Stores the history of your entities (sensors, lights, etc.).

| Column       | Type             | Description                                      |
| :----------- | :--------------- | :----------------------------------------------- |
| `time`       | `TIMESTAMPTZ`    | Timestamp of the state change (Partition Key)    |
| `entity_id`  | `TEXT`           | Entity ID (e.g., `sensor.temperature`)           |
| `state`      | `TEXT`           | Raw state value (e.g., "20.5", "on")             |
| `value`      | `DOUBLE PRECISION`| Numeric value of the state (NULL if non-numeric) |
| `attributes` | `JSONB`          | Full attributes (friendly_name, unit, etc.)      |

### `events` Table
Stores system events (automation triggers, service calls, etc.).

| Column            | Type             | Description                                      |
| :---------------- | :--------------- | :----------------------------------------------- |
| `time`            | `TIMESTAMPTZ`    | Timestamp of the event (Partition Key)           |
| `event_type`      | `TEXT`           | Type of event (e.g., `call_service`)             |
| `event_data`      | `JSONB`          | Full event data payload                          |
| `origin`          | `TEXT`           | Origin of the event (LOCAL, REMOTE)              |
| `context_id`      | `TEXT`           | Context ID for tracing                           |
| `context_user_id` | `TEXT`           | User ID who triggered the event                  |

## 🔍 Available Sensors

Scribe provides several sensors to monitor its performance and storage usage (if `enable_statistics` is true):

*   **States Written**: Total number of state changes recorded.
*   **Events Written**: Total number of events recorded.
*   **States Size (Before Compression)**: Estimated size of the states table if it were uncompressed.
*   **States Size (After Compression)**: Actual size of the states table on disk.
*   **Events Size (Before Compression)**: Estimated size of the events table if it were uncompressed.
*   **Events Size (After Compression)**: Actual size of the events table on disk.
*   **States Compression Ratio**: Percentage of space saved by compression.
*   **Buffer Size**: Number of events currently in the memory buffer.
*   **Last Write Duration**: Time taken to perform the last database write.



## ❓ Troubleshooting

*   **Connection Failed**: Check your DB credentials and ensure the PostgreSQL server allows connections from the HA IP.
*   **Missing Data**: Verify that `record_states` or `record_events` is enabled and that you haven't excluded the entities.
*   **Extension Missing**: Ensure `timescaledb` extension is installed on your Postgres server (`CREATE EXTENSION IF NOT EXISTS timescaledb;`).

For more detailed information, see [TECHNICAL_DOCS.md](TECHNICAL_DOCS.md).

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
