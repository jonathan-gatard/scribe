**🇬🇧 English** · [🇫🇷 Français](README.fr.md) · [🇪🇸 Español](README.es.md) · [🇩🇪 Deutsch](README.de.md)

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
- [Storage tuning](#storage-tuning)
- [Retention](#retention)
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
- ✅ **Tested against a real database**: ~90% line coverage, and an end-to-end suite that drives the integration against a real TimescaleDB rather than mocks.

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

> [!IMPORTANT]
> **The TimescaleDB extension is required.** Chunking, compression, retention
> and the size sensors are the whole point of Scribe, and none of them exist on
> plain PostgreSQL. A new installation is refused if the extension is missing —
> although Scribe enables it for you when the server has it available and your
> database user holds `CREATE` on the database, which the setup below grants.
> Installations that already run without it keep recording and are told what
> they are missing through a Repairs issue.

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
  retention_states: ""   # empty = keep forever
  retention_events: ""   # empty = keep forever
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
| `chunk_time_interval` | How much time each chunk of the table covers. See [Storage tuning](#storage-tuning). |
| `compress_after` | Chunks older than this are compressed. See [Storage tuning](#storage-tuning). |
| `retention_states` | **Deletes** state history older than this interval (e.g. `"365 days"`). Empty (default) keeps everything. See [Retention](#retention). |
| `retention_events` | **Deletes** event history older than this interval. Empty (default) keeps everything. See [Retention](#retention). |
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

## Storage tuning

Scribe stores history in TimescaleDB **hypertables**: a table that looks and
queries like any other, but is physically split into **chunks**, each covering a
slice of time. Almost everything about Scribe's disk usage and query speed comes
down to that split — a query for last week reads only the chunks that overlap
last week, compression works one chunk at a time, and [retention](#retention)
deletes whole chunks rather than individual rows.

Two settings control it, both in YAML and in the UI under **Configure →
Advanced (TimescaleDB & SSL)**:

### `chunk_time_interval` (default `7 days`)

How much time one chunk covers.

- **Smaller chunks** (e.g. `1 day`) mean more, smaller files: finer-grained
  retention, and queries over short recent windows touch less data. Past a
  point, a query spanning months has to open hundreds of chunks.
- **Larger chunks** (e.g. `30 days`) mean fewer, bigger files: better for long
  historical queries, worse for memory — TimescaleDB's own guidance is that the
  chunks you write to should comfortably fit in memory alongside their indexes,
  so an oversized chunk on a small machine hurts write performance.

The default suits a typical Home Assistant instance. Consider `1 day` if you
record thousands of entities, and only then.

> **Changing it affects new chunks only.** Chunks already written keep the span
> they were created with, and nothing is rewritten or moved — you will simply
> have a mix of old and new spans, which TimescaleDB handles natively.

### `compress_after` (default `7 days`)

How old a chunk must be before TimescaleDB compresses it. Compression is
typically a large reduction in size for this kind of data (many repeated
`entity_id`s and slowly-changing values), which is why it is on by default.

Compressed chunks are still fully queryable — the `states` view does not care.
Writing *into* one is slower, which is why compression only kicks in once a
chunk is old enough to be effectively finished. Keep `compress_after` comfortably
above the age of the data you still write to; states arriving out of order (a
backfill, a migration script) land in old chunks.

> **Changing it takes effect on the next restart**, and chunks already
> compressed stay compressed — the setting only decides when the *next* ones
> are.

### How the three settings fit together

| Setting | What it does | Reversible |
| :--- | :--- | :--- |
| `chunk_time_interval` | How much time one chunk covers | Yes — future chunks only |
| `compress_after` | When a chunk gets compressed | Yes |
| `retention_states` / `retention_events` | When a chunk gets **deleted** | **No** |

They apply in that order to the same chunk over its life: written → compressed →
dropped. Two consequences worth knowing:

- If `compress_after` is greater than your retention, chunks are deleted before
  they are ever compressed, and compression does nothing.
- Retention deletes whole chunks, so your real retention window is the interval
  you set *plus* up to one `chunk_time_interval`. Smaller chunks make it tighter.

If the size and chunk statistics sensors are enabled (`enable_stats_size`,
`enable_stats_chunk`), they report exactly what these settings produce: chunk
counts, compressed and uncompressed sizes, and the compression ratio.

## Retention

By default Scribe keeps everything, forever. If you only want to store a bounded
window — because you aggregate the raw history elsewhere, or simply to cap disk
usage — set a retention interval and TimescaleDB will drop chunks older than it:

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  retention_states: "365 days"
  retention_events: "30 days"
```

Both are also in the UI under **Configure → Advanced (TimescaleDB & SSL)**.

> [!WARNING]
> Retention **deletes data permanently**. There is no undo and no trip to a bin:
> once a chunk falls outside the window it is dropped, and only a backup brings
> it back. States and events are configured separately so you can expire noisy
> events while keeping state history.

Details worth knowing:

- **No setting means "keep forever", always.** Clearing the field in the UI and
  deleting the line from `configuration.yaml` both remove the policy — a value
  Scribe once imported from YAML is never allowed to outlive the line that set
  it.
- **Scribe owns the retention policy on its own tables.** Emptying the field
  removes the policy — including one you created by hand with
  `add_retention_policy()`, which is the only way clearing the setting in the UI
  can actually stop the deletions.
- **It starts immediately.** TimescaleDB runs the policy within seconds of it
  being created, not at the next daily interval — everything outside the window
  is gone on the first run, right after the restart that enabled it.
- **Deletion happens by chunk, not by row.** A chunk is dropped only once *all*
  of its rows are older than the interval, so with the default `chunk_time_interval`
  of 7 days you keep up to a week more than you asked for. That is what makes
  retention nearly free: it drops files rather than deleting rows.
- **Only the history is deleted.** The `entities` table and the other metadata
  tables are not touched, so an entity whose history has fully expired still
  resolves.
- **TimescaleDB is required** — it is the extension that runs the policy. On
  plain PostgreSQL, setting a retention interval raises a Repairs issue instead
  of silently doing nothing.
- Accepted values are plain intervals: `30 days`, `6 months`, `1 year`.
  Anything else is refused with an error rather than sent to the database.

## Migration

### Upgrading from Scribe 2.x

Scribe 3.0 replaced the `states` table with `states_raw` plus a compatibility
view, and gave `entities` a numeric primary key. The conversion of an old
database was carried by 3.x and **removed in 3.9**.

If your database still has a `states` *table* (rather than a view), a
`states_legacy` table, or an `entities` table without an `id` column, Scribe
stops at startup, records nothing, and raises a Repairs issue — without
renaming, creating or deleting anything. Install **Scribe 3.8**, let Home
Assistant run until the logs report the migration finished (around fifteen
minutes on a large database), then update again.

Fresh installs and any database created by 3.x are unaffected.

### Backfilling from other sources

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

### Before anything else

Two places answer "why is nothing being recorded?" without reading a single log line:

- **Settings → Devices & Services → Scribe → ⋮ → Download diagnostics** reports what
  the writer is actually doing: connected or not, whether TimescaleDB was found, how
  many items are waiting in the buffer and how many were dropped, consecutive write
  failures, the storage and retention settings in force. The database URL is never
  included, and driver errors have any connection string stripped out.
- **Settings → System → Repairs** lists the conditions below, and
  **Settings → System → System health** shows the database Scribe is pointed at and
  whether it is connected right now.

### Repairs

Scribe reports problems it cannot fix on its own in **Settings → System → Repairs**, so you do not have to watch the logs. Each one disappears by itself once the condition is resolved.

| Repair | What it means |
| --- | --- |
| Cannot reach its database | The connection failed. Scribe keeps buffering and retries in the background, so history recorded during the outage is written once the database returns. Check that the server is up and that the URL and credentials are right. |
| Cannot write to its database | Several consecutive writes failed. Data is held in memory and written on recovery — unless Home Assistant restarts first. |
| Buffer is full | Writes failed long enough to saturate the buffer; the oldest records are now being discarded. Fix the database, or raise `max_queue_size`. |
| Discarding records | A write failed while buffering is disabled, so records were dropped immediately. Enable buffering to survive short outages. |
| Could not create its tables | Scribe reached the database but failed to build its schema, usually a privileges problem. On a new database nothing is recorded at all. |
| Could not create the `states` view | History is recorded, but the view every query goes through is missing — the history looks empty even though nothing is lost. |
| `states_raw` / `events` is not a hypertable | TimescaleDB is installed but the table was never converted (a common outcome when the extension is added *after* the tables filled up). Chunking, compression and retention all do nothing. |
| `states_raw` / `events` is never compressed | The table is a hypertable but has no compression policy, so it keeps its full uncompressed size. |
| TLS is not fully in force | Scribe connects over TLS, but a certificate you configured could not be applied — most often a client certificate, so it authenticates as an ordinary client instead of the one you provisioned. |
| TimescaleDB is not installed | History is recorded, but chunking and compression are unavailable, so the database grows much faster and the size sensors stay empty. |
| Database predates version 3.0 | The database still uses the pre-3.0 layout, which this version cannot convert. Nothing is recorded and nothing was modified — install Scribe 3.8 to convert it, then update again. |
| Could not apply the retention policy | You asked Scribe to delete data older than an interval and the policy could not be created. Nothing was deleted, and nothing is being deleted — the table keeps growing. |
| Entity rename was not applied | A rename collided with an existing row in the database. The entity's history is split across the two IDs. |

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
