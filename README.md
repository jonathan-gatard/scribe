# Chronicle

**Chronicle** is a high-performance custom component for Home Assistant that stores your history and long-term statistics in **TimescaleDB**.

It is designed to be a lightweight, "set-and-forget" alternative to the built-in Recorder or other custom components like LTSS.

## 🚀 Features

- **UI Configuration**: Fully configurable via Home Assistant's Integrations page.
- **Entity Filtering**: Select exactly which entities or domains to record via the UI.
- **Automatic Hypertables**: Automatically converts your table to a TimescaleDB hypertable.
- **Native Compression**: Automatically enables TimescaleDB compression to save massive amounts of disk space (up to 95%).
- **Performance**: Uses `SQLAlchemy` and batch inserts for minimal impact on Home Assistant's performance.

## 📦 Installation

### Option 1: HACS (Recommended)
1.  Open HACS.
2.  Go to "Integrations" > "Custom repositories".
3.  Add `https://github.com/jonathan-gatard/chronicle` as an Integration.
4.  Click "Download".
5.  Restart Home Assistant.

### Option 2: Manual
1.  Copy the `custom_components/chronicle` folder to your Home Assistant `config/custom_components/` directory.
2.  Restart Home Assistant.

## ⚙️ Configuration

1.  Go to **Settings > Devices & Services**.
2.  Click **Add Integration**.
3.  Search for **Chronicle**.
4.  Enter your PostgreSQL / TimescaleDB connection details:
    - **Database URL**: `postgresql://user:password@host:5432/dbname`
    - **Table Name**: `chronicle_events` (default)
    - **Chunk Interval**: `7 days` (default)
    - **Compress After**: `14 days` (default)

### Options
Click "Configure" on the integration card to change settings later:
- **Include/Exclude Domains**: Filter by domain (e.g., `sensor`, `switch`).
- **Include/Exclude Entities**: Filter specific entities.
- **Compression Settings**: Adjust chunk intervals and compression policies.

## 📊 Database Schema

Chronicle creates a single table (default: `chronicle_events`) with the following schema:

| Column       | Type             | Description                                      |
| :----------- | :--------------- | :----------------------------------------------- |
| `time`       | `TIMESTAMPTZ`    | Timestamp of the event (indexed, partitioning key)|
| `entity_id`  | `TEXT`           | Entity ID (e.g., `sensor.temperature`)           |
| `state`      | `TEXT`           | Raw state value                                  |
| `value`      | `DOUBLE PRECISION`| Numeric value of the state (NULL if non-numeric) |
| `attributes` | `JSONB`          | Full attributes of the state                     |

## 📈 Grafana Example

Visualize your data in Grafana using PostgreSQL/TimescaleDB as the data source:

```sql
SELECT
  time AS "time",
  value
FROM chronicle_events
WHERE
  entity_id = 'sensor.temperature'
  AND $__timeFilter(time)
ORDER BY time
```
