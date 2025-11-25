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
- **UI Configuration**: Fully managed via Home Assistant's Integrations page.

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
    - **Database URL**: `postgresql://user:password@host:5432/dbname`
    - **Chunk Interval**: `30 days` (default)
    - **Compress After**: `7 days` (default)
    - **Record States**: Enable to record sensor history (default: True).
    - **Record Events**: Enable to record automation triggers, service calls, etc. (default: False).

### Advanced Configuration (YAML)
Scribe supports full configuration via `configuration.yaml`. This is useful if you prefer "Infrastructure as Code" or want to configure advanced settings not available in the initial setup flow.

```yaml
scribe:
  db_url: postgresql://user:password@host:5432/dbname
  chunk_time_interval: 30 days
  compress_after: 7 days
  record_states: true
  record_events: false
  batch_size: 100        # Number of events to buffer before writing
  flush_interval: 5      # Seconds to wait before flushing buffer
  table_name_states: states   # Custom table name for states
  table_name_events: events   # Custom table name for events
  include_domains:
    - sensor
    - switch
  exclude_entities:
    - sensor.noisy_sensor
```

**Note**: If you configure Scribe via YAML, the settings will be imported into the UI config entry. You can still modify them later via the UI "Configure" button.

## 📊 Database Schema

Scribe creates two tables (if enabled): `states` and `events`.

### `states` Table
Stores the history of your entities (sensors, lights, etc.).

| Column       | Type             | Description                                      |
| :----------- | :--------------- | :----------------------------------------------- |
| `time`       | `TIMESTAMPTZ`    | Timestamp of the state change (Primary Key)      |
| `entity_id`  | `TEXT`           | Entity ID (e.g., `sensor.temperature`)           |
| `state`      | `TEXT`           | Raw state value (e.g., "20.5", "on")             |
| `value`      | `DOUBLE PRECISION`| Numeric value of the state (NULL if non-numeric) |
| `attributes` | `JSONB`          | Full attributes (friendly_name, unit, etc.)      |

### `events` Table
Stores system events (automation triggers, service calls, etc.).

| Column            | Type             | Description                                      |
| :---------------- | :--------------- | :----------------------------------------------- |
| `time`            | `TIMESTAMPTZ`    | Timestamp of the event (Primary Key)             |
| `event_type`      | `TEXT`           | Type of event (e.g., `call_service`)             |
| `event_data`      | `JSONB`          | Full event data payload                          |
| `origin`          | `TEXT`           | Origin of the event (LOCAL, REMOTE)              |
| `context_id`      | `TEXT`           | Context ID for tracing                           |
| `context_user_id` | `TEXT`           | User ID who triggered the event                  |

## 📈 Grafana Examples

### Plot a Sensor Value
```sql
SELECT
  time AS "time",
  value
FROM states
WHERE
  entity_id = 'sensor.temperature'
  AND $__timeFilter(time)
ORDER BY time
```

### Query Attributes (JSONB)
Extract specific attributes like battery level:
```sql
SELECT
  time AS "time",
  (attributes->>'battery_level')::float as battery
FROM states
WHERE
  entity_id = 'sensor.motion_sensor'
  AND attributes->>'battery_level' IS NOT NULL
  AND $__timeFilter(time)
ORDER BY time
```

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
