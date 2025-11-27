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

### 1. Database Setup

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

### 2. Install Component

**HACS (Recommended)**
1. Add this repository as a custom repository in HACS.
2. Search for "Scribe" and install.
3. Restart Home Assistant.

**Manual**
1. Copy the `custom_components/scribe` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

## Configuration

### Minimal Configuration

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
```

### Complete Configuration

```yaml
scribe:
  # Database connection (REQUIRED)
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_ssl: false  # Enable SSL/TLS
  
  # TimescaleDB settings
  chunk_time_interval: "7 days"   # Chunk size
  compress_after: "60 days"       # Compression policy
  
  # What to record
  record_states: true
  record_events: false
  
  # Performance tuning
  batch_size: 100           # Items per write
  flush_interval: 5         # Seconds between flushes
  max_queue_size: 10000     # Max buffer size
  buffer_on_failure: true   # Buffer writes on DB failure
  
  # Statistics sensors (all optional, default: false)
  enable_stats_io: false         # Real-time writer metrics
  enable_stats_chunk: false      # Chunk count statistics
  enable_stats_size: false       # Storage size statistics
  stats_chunk_interval: 60      # Minutes between chunk stats updates
  stats_size_interval: 60       # Minutes between size stats updates
  
  # Filtering
  include_domains:
    - sensor
    - light
  include_entities:
    - sensor.temperature
  exclude_domains:
    - automation
  exclude_entities:
    - sensor.noisy_sensor
  exclude_attributes:
    - entity_picture
```

## Statistics Sensors

Enable sensors by setting their flags in your configuration:

### IO Statistics (`enable_stats_io: true`)
Real-time metrics from the writer (no DB queries):
- `sensor.scribe_states_written` - Total states written
- `sensor.scribe_events_written` - Total events written
- `sensor.scribe_buffer_size` - Current queue size
- `sensor.scribe_write_duration` - Last write duration (seconds)

### Chunk Statistics (`enable_stats_chunk: true`)
Chunk counts (updated every N minutes):
- `sensor.scribe_states_total_chunks`
- `sensor.scribe_states_compressed_chunks`
- `sensor.scribe_states_uncompressed_chunks`
- `sensor.scribe_events_total_chunks`
- `sensor.scribe_events_compressed_chunks`
- `sensor.scribe_events_uncompressed_chunks`

### Size Statistics (`enable_stats_size: true`)
Storage usage in bytes (updated every N minutes):
- `sensor.scribe_states_total_size`
- `sensor.scribe_states_compressed_size`
- `sensor.scribe_states_uncompressed_size`
- `sensor.scribe_events_total_size`
- `sensor.scribe_events_compressed_size`
- `sensor.scribe_events_uncompressed_size`

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

### Statistics not updating
- Ensure coordinator flags are enabled (`enable_stats_chunk`, `enable_stats_size`)
- Check update intervals aren't too long
- View Home Assistant logs for coordinator errors

### Still having issues?
Please [open an issue](https://github.com/jonathan-gatard/scribe/issues) on GitHub with your logs and configuration. We're happy to help!

## License

MIT License - See LICENSE file for details
