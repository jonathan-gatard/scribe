> [!WARNING]
> # ⚠️ v3.x.x Database Schema Migration
> 
> **Version 3.x.x introduces a major database schema optimization (`states_raw` table).**
>
> *This only applies if you are upgrading from a previous version of Scribe*
> 
> *The automated background migration ran perfectly on several production setup without any issues.*
> **However, it is strongly recommended to perform a database backup before updating.**
>

# Scribe - High-Performance TimescaleDB Integration for Home Assistant

Scribe is a next-generation component that writes Home Assistant states and events to a TimescaleDB database. 

**Why Scribe?**
Scribe is built differently. Unlike other integrations that rely on synchronous drivers or the default recorder, Scribe uses **`asyncpg`**, a high-performance asynchronous PostgreSQL driver. This allows it to handle massive amounts of data without blocking Home Assistant's event loop. It's designed for stability, speed, and efficiency.

**Data structure and query**

An explanation of the data structure how to query can be found here: [Data structure](datastructre.md)

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Migration](#migration)
- [Statistics Sensors](#statistics-sensors)
- [Services](#services)
- [Dashboard / View](#dashboard--view)
- [Ecosystem / Related Projects](#ecosystem--related-projects)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Features

- 🚀 **Async-First Architecture**: Built on `asyncpg` for non-blocking, high-throughput writes.
- 📦 **TimescaleDB Native**: Automatically manages Hypertables and Compression Policies.
- 📊 **Granular Statistics**: Optional sensors for monitoring chunk counts, compression ratios (up to 97% saved!), and I/O performance.
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

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jonathan-gtd&repository=scribe&category=integration)

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
  include_events: []
  exclude_events: []
  # Optional: Disable specific metadata tables (default: true)
  enable_table_areas: true
  enable_table_devices: true
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
| `include_events` | List of event types to record. Leave empty to record all events. |
| `exclude_events` | List of event types to never record (applied after `include_events`). |
| `enable_table_areas` | Enable creation and sync of the `areas` table. |
| `enable_table_devices` | Enable creation and sync of the `devices` table. |
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

### Recorder Migration

<details>
<summary><b>Show Recorder Migration Guide</b></summary>

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
   # Fill in [Recorder Configuration], [Scribe Configuration], and [Migration Settings]
   ```

4. Run the migration:
   ```bash
   python3 recorder2scribe.py
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
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Total disk size (includes compressed data + recent chunks + indices). |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_states_original_size` | **Theoretical size** if data was not compressed (e.g. 11 GB). |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Physical size of the compressed data chunks. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Size of recent data not yet compressed (or pending indices). |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compression_ratio` | Compression ratio for states (%). |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Total disk size of the events table. |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_events_original_size` | Theoretical size of events before compression. |
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

### High memory usage
- Reduce `max_queue_size`
- Reduce `flush_interval` for faster writes
- Check `sensor.scribe_buffer_size`

### Performance tuning

If the `states` view is slow (several seconds per query), it is likely due to the PostgreSQL query planner choosing a **Hash Join** instead of a **Nested Loop**, which prevents TimescaleDB from pruning chunks effectively.

The most common cause is a high `random_page_cost` (the default is `4.0`, optimized for HDDs). If you are using modern storage (SSD, NVMe) or have a well-cached database, you should reduce this value:

```sql
-- Check current value
SHOW random_page_cost;

-- Set to a lower value (usually 1.1)
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

Reducing this value encourages the planner to use index-based joins (Nested Loops), which are essential for Scribe's performance with large datasets.

### Still having issues?
Please [open an issue](https://github.com/jonathan-gtd/scribe/issues) on GitHub with your logs and configuration. I would be happy to help!

## Dashboard / View

A pre-configured Lovelace layout containing all useful Scribe sensors (Database Statistics, Compression Ratios, I/O Performance) is available in this repository, in two flavours:

| File | What it is | Where to paste it |
| --- | --- | --- |
| [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml) | A **single card** (`type: vertical-stack`) | The card YAML editor ("Add card" → "Manual") |
| [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml) | A **whole view** (`title` / `icon` / `cards`) | The view YAML editor |

> ⚠️ These two are not interchangeable. Pasting the *view* file into a *card* editor fails with **"No card type configured"**, because a card config must start with a `type:` key.

**Option A — add it as a card (easiest, works in every view type):**

1.  Open your dashboard and click "Edit Dashboard" (pencil icon).
2.  Click **+ Add card** and scroll to the bottom of the card picker to select **Manual**.
3.  Copy the content of [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml), replace everything in the editor with it, and click **Save**.

**Option B — add it as a dedicated view:**

1.  Open your dashboard and click "Edit Dashboard" (pencil icon).
2.  Click the **+** button *in the top tab bar* (next to your existing view names) to add a new View — not the "Add card" button.
3.  In the view dialog, open the ⋮ menu (or the "Show code editor" button) and choose **Edit in YAML**.
4.  Copy the content of [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml), replace everything in the editor with it, and click **Save**.



## Ecosystem / Related Projects

Check out these related projects that work great with Scribe:

- [timescale_database_reader](https://github.com/remmob/timescale_database_reader): A custom component to read data back from TimescaleDB into Home Assistant sensors.
- [timescale-plotly-card](https://github.com/remmob/timescale-plotly-card): A highly customizable Plotly-based card for Home Assistant that can query TimescaleDB directly.

## License

MIT License - See LICENSE file for details
