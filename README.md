# Scribe - High-Performance TimescaleDB Integration for Home Assistant

Scribe is a next-generation component that writes Home Assistant states and events to a TimescaleDB database. 

**Why Scribe?**
Scribe is built differently. Unlike other integrations that rely on synchronous drivers or the default recorder, Scribe uses **`asyncpg`**, a high-performance asynchronous PostgreSQL driver. This allows it to handle massive amounts of data without blocking Home Assistant's event loop. It's designed for stability, speed, and efficiency.


## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Migration](#migration)
- [Statistics Sensors](#statistics-sensors)
- [Services](#services)
- [Dashboard / View](#dashboard--view)
- [Troubleshooting](#troubleshooting)
- [Technical Data](#technical-data)
- [License](#license)

## Features

- 🚀 **Async-First Architecture**: Built on `asyncpg` for non-blocking, high-throughput writes.
- 📦 **TimescaleDB Native**: Automatically manages Hypertables and Compression Policies.
- 📊 **Granular Statistics**: Optional sensors for monitoring chunk counts, compression ratios, and I/O performance.
- 🔒 **Secure**: Full SSL/TLS support.
- 📈 **States & Events**: Records all state changes and events to `states` and `events` tables.
- 👥 **User Context**: Automatically syncs Home Assistant users to the database for rich context.
- 🧩 **Entity Metadata**: Automatically syncs entity registry (names, platforms, etc.) to the `entities` table.
- 🏠 **Area & Device Context**: Automatically syncs areas and devices to `areas` and `devices` tables.
- 🔌 **Integration Info**: Automatically syncs integration config entries to the `integrations` table.
- 🎯 **Smart Filtering**: Include/exclude by domain, entity, entity glob, or attribute.
- ✅ **100% Test Coverage**: Robust and reliable.

## Installation

### 1. Install Component

**HACS (Recommended)**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jonathan-gatard&repository=scribe&category=integration)

1. Add this repository as a custom repository in HACS.
2. Search for "Scribe" and install.
3. Restart Home Assistant.

**Manual**
1. Copy the `custom_components/scribe` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

### 2. Database Setup

You need a running TimescaleDB instance. I recommend PostgreSQL 17 or 18.

#### Option A: Home Assistant OS (Add-on)

If you are running Home Assistant OS, I recommend using the [TimescaleDB Add-on](https://github.com/expaso/hassos-addon-timescaledb).

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fexpaso%2Fhassos-addon-timescaledb)

#### Option B: Docker (Manual)

```bash
# High Availability (Recommended)
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb-ha:pg18

# Standard
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb:pg18
```

Create the database and user:

```sql
CREATE DATABASE scribe;
CREATE USER scribe WITH PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE scribe TO scribe;

\c scribe
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT ALL ON SCHEMA public TO scribe;
```

## Configuration

### Minimal Configuration

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
```

### Full Configuration (Default Values)

<details>
<summary><b>Show Full YAML Configuration</b></summary>

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_ssl: false
  chunk_time_interval: "7 days"
  compress_after: "7 days"
  record_states: true
  record_events: false
  batch_size: 500
  flush_interval: 5
  max_queue_size: 10000
  buffer_on_failure: true
  enable_stats_io: false
  enable_stats_chunk: false
  enable_stats_size: false
  stats_chunk_interval: 60
  stats_size_interval: 60
  include_domains: []
  include_entity_globs: []
  exclude_domains: []
  exclude_entities: []
  exclude_entity_globs: []
  exclude_attributes: []
  # Optional: Disable specific metadata tables (default: true)
  enable_table_areas: true
  enable_table_devices: true
  enable_table_entities: true
  enable_table_integrations: true
  enable_table_users: true
```
</details>

### Configuration Parameters

<details>
<summary><b>Show Parameter Reference</b></summary>

| Parameter | Description |
| :--- | :--- |
| `db_url` | **Required.** The connection string for your TimescaleDB database. |
| `db_ssl` | Enable SSL/TLS for the database connection. |
| `chunk_time_interval` | The duration of each data chunk in TimescaleDB. |
| `compress_after` | How old data should be before it is compressed. |
| `record_states` | Whether to record state changes. |
| `record_events` | Whether to record events. |
| `batch_size` | Number of items to buffer before writing to the database. |
| `flush_interval` | Maximum time (in seconds) to wait before flushing the buffer. |
| `max_queue_size` | Maximum number of items to hold in memory before dropping new ones. |
| `buffer_on_failure` | If true, keeps data in memory if the DB is unreachable (up to `max_queue_size`). |
| `enable_stats_io` | Enable real-time writer performance sensors (no DB queries). |
| `enable_stats_chunk` | Enable chunk count statistics sensors (queries DB). |
| `enable_stats_size` | Enable storage size statistics sensors (queries DB). |
| `stats_chunk_interval` | Interval (in minutes) to update chunk statistics. |
| `stats_size_interval` | Interval (in minutes) to update size statistics. |
| `include_domains` | List of domains to include. |
| `include_entities` | List of specific entities to include. |
| `include_entity_globs` | List of entity patterns to include (e.g. `sensor.weather_*`). |
| `exclude_domains` | List of domains to exclude. |
| `exclude_entities` | List of specific entities to exclude. |
| `exclude_entity_globs` | List of entity patterns to exclude (e.g. `switch.kitchen_*`). |
| `exclude_attributes` | List of attributes to exclude from the `attributes` column. |
| `enable_table_areas` | Enable creation and sync of the `areas` table. |
| `enable_table_devices` | Enable creation and sync of the `devices` table. |
| `enable_table_entities` | Enable creation and sync of the `entities` table. |
| `enable_table_integrations` | Enable creation and sync of the `integrations` table. |
| `enable_table_users` | Enable creation and sync of the `users` table. |
</details>

## Migration

Scribe provided helper scripts to backfill data from various sources.

### InfluxDB Migration

<details>
<summary><b>Show InfluxDB Migration Guide</b></summary>

1. Navigate to the `migration` directory:
   ```bash
   cd migration
   ```

2. Install dependencies:
   ```bash
   pip install influxdb-client psycopg2-binary python-dotenv
   ```

3. Configure the migration:
   ```bash
   cp .env.example .env
   nano .env
   # Fill in [InfluxDB Configuration], [Scribe Configuration], and [Migration Settings]
   ```

4. Run the migration:
   ```bash
   python3 influx2scribe.py
   ```
</details>

### LTSS Migration

<details>
<summary><b>Show LTSS Migration Guide</b></summary>

1. Navigate to the `migration` directory:
   ```bash
   cd migration
   ```

2. Install dependencies:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Configure the migration:
   ```bash
   cp .env.example .env
   nano .env
   # Fill in [LTSS Configuration], [Scribe Configuration], and [Migration Settings]
   ```

4. Run the migration:
   ```bash
   python3 ltss2scribe.py
   ```
</details>

## Statistics Sensors

Enable sensors by setting their flags in your configuration.

### IO Statistics (`enable_stats_io: true`)

<details>
<summary><b>Show IO Sensors</b></summary>

Real-time metrics from the writer (no DB queries).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_states_written` | Total number of state changes written to the DB. |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_events_written` | Total number of events written to the DB. |
| <img src="https://api.iconify.design/mdi:buffer.svg?color=%232196F3" width="15" /> `sensor.scribe_buffer_size` | Current number of items waiting in the memory buffer. |
| <img src="https://api.iconify.design/mdi:timer-sand.svg?color=%232196F3" width="15" /> `sensor.scribe_write_duration` | Time taken (in ms) for the last database write operation. |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_states_rate` | Rate of states written to DB (per minute). |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_events_rate` | Rate of events written to DB (per minute). |
</details>

### Chunk Statistics (`enable_stats_chunk: true`)

<details>
<summary><b>Show Chunk Sensors</b></summary>

Chunk counts (updated every `stats_chunk_interval` minutes).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_chunks` | Total number of chunks for the states table. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_chunks` | Number of chunks that have been compressed. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_chunks` | Number of chunks waiting to be compressed. |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_chunks` | Total number of chunks for the events table. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_chunks` | Number of compressed event chunks. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_chunks` | Number of uncompressed event chunks. |
</details>

### Size Statistics (`enable_stats_size: true`)

<details>
<summary><b>Show Size Sensors</b></summary>

Storage usage in bytes (updated every `stats_size_interval` minutes).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Total disk size of the states table. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Size of compressed state data. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Size of uncompressed state data. |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compression_ratio` | Compression ratio for states (%). |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Total disk size of the events table. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_size` | Size of compressed event data. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_size` | Size of uncompressed event data. |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compression_ratio` | Compression ratio for events (%). |
</details>

## Services

### `scribe.flush`
Force an immediate flush of buffered data to the database.

```yaml
service: scribe.flush
```

### `scribe.query`
Execute a read-only SQL query against the TimescaleDB database.

**Parameters:**
- `sql` (Required): The SQL query to execute. Must be a `SELECT` statement.

**Returns:**
A list of rows, where each row is a dictionary of column names and values.

**Example:**
```yaml
service: scribe.query
data:
  sql: "SELECT * FROM states ORDER BY time DESC LIMIT 5"
response_variable: query_result
```

## Troubleshooting

### No data being written
1. Check Home Assistant logs for errors
2. Verify database connection with `psql -U scribe -h host -d scribe`
3. Enable `enable_stats_io: true` to monitor buffer and writes
4. Check `sensor.scribe_buffer_size` - if it's growing, there's a DB issue

### High memory usage
- Reduce `max_queue_size`
- Reduce `flush_interval` for faster writes
- Check `sensor.scribe_buffer_size`

### SSL Configuration

You can configure SSL certificates for the database connection. This is useful if your database requires client certificate authentication or if you need to verify the server's certificate.

*   **SSL Root Certificate**: Path to the CA certificate (e.g., `certs/root.crt`).
*   **SSL Certificate File**: Path to the client certificate (e.g., `certs/client.crt`).
*   **SSL Key File**: Path to the client key (e.g., `certs/client.key`).

**Note on Paths**: You can use absolute paths or paths relative to your Home Assistant configuration directory. For example, if you create a `certs` folder in your config directory, you can simply use `certs/client.crt`.

### Statistics not updating
- Ensure coordinator flags are enabled (`enable_stats_chunk`, `enable_stats_size`)
- Check update intervals aren't too long
- View Home Assistant logs for coordinator errors

### Still having issues?
Please [open an issue](https://github.com/jonathan-gatard/scribe/issues) on GitHub with your logs and configuration. I would be happy to help!

## Dashboard / View

A pre-configured Lovelace view containing all useful Scribe sensors (Database Statistics, Compression Ratios, I/O Performance) is available in this repository.

**To add it to your Home Assistant dashboard:**

1.  Open your dashboard and click "Edit Dashboard" (pencil icon).
2.  Click the **+** button to add a new View.
3.  Select **YAML Mode** (or "Edit in YAML").
4.  Copy the content of [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml) and paste it into the editor.




## Technical Data

For detailed technical information about the architecture, database schema, and data flow, please refer to the [Technical Documentation](TECHNICAL_DOCS.md).

## License

MIT License - See LICENSE file for details
