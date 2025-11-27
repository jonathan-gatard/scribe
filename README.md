# Scribe - High-Performance TimescaleDB Integration for Home Assistant

Scribe is a next-generation component that writes Home Assistant states and events to a TimescaleDB database. 

**Why Scribe?**
Scribe is built differently. Unlike other integrations that rely on synchronous drivers or the default recorder, Scribe uses **`asyncpg`**, a high-performance asynchronous PostgreSQL driver. This allows it to handle massive amounts of data without blocking Home Assistant's event loop. It's designed for stability, speed, and efficiency.

## Features

- 🚀 **Async-First Architecture**: Built on `asyncpg` for non-blocking, high-throughput writes.
- 📦 **TimescaleDB Native**: Automatically manages Hypertables and Compression Policies.
- 📊 **Granular Statistics**: Optional sensors for monitoring chunk counts, compression ratios, and I/O performance.
- 🔒 **Secure**: Full SSL/TLS support.
- 🎯 **Smart Filtering**: Include/exclude by domain, entity, or attribute.
- ✅ **100% Test Coverage**: Robust and reliable.

## Installation

### 1. Install Component

**HACS (Recommended)**
1. Add this repository as a custom repository in HACS.
2. Search for "Scribe" and install.
3. Restart Home Assistant.

**Manual**
1. Copy the `custom_components/scribe` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

### 2. Database Setup

You need a running TimescaleDB instance. We recommend PostgreSQL 17.

```bash
# High Availability (Recommended)
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb-ha:pg17

# Standard
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb:pg17
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

### Configuration Parameters

| Parameter | Description | Example |
| :--- | :--- | :--- |
| `db_url` | **Required.** The connection string for your TimescaleDB database. | `postgresql://user:pass@host:5432/db` |
| `db_ssl` | Enable SSL/TLS for the database connection. | `false` |
| `chunk_time_interval` | The duration of each data chunk in TimescaleDB. | `"7 days"` |
| `compress_after` | How old data should be before it is compressed. | `"60 days"` |
| `record_states` | Whether to record state changes. | `true` |
| `record_events` | Whether to record events. | `false` |
| `batch_size` | Number of items to buffer before writing to the database. | `100` |
| `flush_interval` | Maximum time (in seconds) to wait before flushing the buffer. | `5` |
| `max_queue_size` | Maximum number of items to hold in memory before dropping new ones. | `10000` |
| `buffer_on_failure` | If true, keeps data in memory if the DB is unreachable (up to `max_queue_size`). | `true` |
| `enable_stats_io` | Enable real-time writer performance sensors (no DB queries). | `false` |
| `enable_stats_chunk` | Enable chunk count statistics sensors (queries DB). | `false` |
| `enable_stats_size` | Enable storage size statistics sensors (queries DB). | `false` |
| `stats_chunk_interval` | Interval (in minutes) to update chunk statistics. | `60` |
| `stats_size_interval` | Interval (in minutes) to update size statistics. | `60` |
| `include_domains` | List of domains to include. | `["sensor", "light"]` |
| `include_entities` | List of specific entities to include. | `["sensor.temp"]` |
| `exclude_domains` | List of domains to exclude. | `["automation"]` |
| `exclude_entities` | List of specific entities to exclude. | `["sensor.noisy"]` |
| `exclude_attributes` | List of attributes to exclude from the `attributes` column. | `["entity_picture"]` |

## Statistics Sensors

Enable sensors by setting their flags in your configuration.

### IO Statistics (`enable_stats_io: true`)

Real-time metrics from the writer (no DB queries).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_states_written` | Total number of state changes written to the DB. |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_events_written` | Total number of events written to the DB. |
| <img src="https://api.iconify.design/mdi:buffer.svg?color=%232196F3" width="15" /> `sensor.scribe_buffer_size` | Current number of items waiting in the memory buffer. |
| <img src="https://api.iconify.design/mdi:timer-sand.svg?color=%232196F3" width="15" /> `sensor.scribe_write_duration` | Time taken (in ms) for the last database write operation. |

### Chunk Statistics (`enable_stats_chunk: true`)

Chunk counts (updated every `stats_chunk_interval` minutes).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_chunks` | Total number of chunks for the states table. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_chunks` | Number of chunks that have been compressed. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_chunks` | Number of chunks waiting to be compressed. |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_chunks` | Total number of chunks for the events table. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_chunks` | Number of compressed event chunks. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_chunks` | Number of uncompressed event chunks. |

### Size Statistics (`enable_stats_size: true`)

Storage usage in bytes (updated every `stats_size_interval` minutes).

| Sensor | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Total disk size of the states table. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Size of compressed state data. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Size of uncompressed state data. |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Total disk size of the events table. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_size` | Size of compressed event data. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_size` | Size of uncompressed event data. |

## Services

### `scribe.flush`
Force an immediate flush of buffered data to the database.

```yaml
service: scribe.flush
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
Please [open an issue](https://github.com/jonathan-gatard/scribe/issues) on GitHub with your logs and configuration. We're happy to help!

## License

MIT License - See LICENSE file for details
