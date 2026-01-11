# Changelog

All notable changes to this project will be documented in this file.

## [2.12.0] - 2026-01-11

### Fixed
- **Metadata Sync**: Fixed entities and integrations table insertion failures (GitHub issue #11).
  - Added type conversion for all text fields to ensure strings before insertion (fixes "expected str, got int" error).
  - Added null byte sanitization to all metadata write methods (fixes "invalid byte sequence for encoding UTF8: 0x00" error).
  - Affected methods: `write_users`, `write_entities`, `write_areas`, `write_devices`, `write_integrations`.
- **Tests**: Updated `get_db_stats` tests to match PR #9 behavior (partial stats with default 0 values instead of empty dict on failure).

## [2.11.3] - 2025-12-19


### Security
- Fix SQL Injection vulnerability in `query` service by enforcing READ ONLY transactions.

### Performance
- Optimize heavy JSON serialization by moving it to an executor thread prevent blocking Home Assistant event loop.

### Fixes
- Fix potential infinite recursion in object sanitization.
- Remove unused `_queue_lock`.


## [2.11.2] - 2025-12-14

### Fixed
- **Encoding**: Fixed `UnicodeEncodeError` by sanitizing inputs and handling untranslatable characters in `writer.py`.

## [2.11.1] - 2025-12-13

### Fixed
- **Documentation**: Updated dashboard and readme with rate sensors instructions.

## [2.11.0] - 2025-12-13

### Added
- **Rate Sensors**: Added `sensor.scribe_states_rate` and `sensor.scribe_events_rate` to monitor database write throughput.
- **UI**: Improved entity selection with UI selectors for filtering.

## [2.10.2] - 2025-12-13

### Changed
- **Cleanup**: Repository cleanup and minor robustness improvements.

## [2.10.1] - 2025-12-13

### Fixed
- **Error Handling**: Improved error logging and sensor stability.

## [2.10.0] - 2025-12-13

### Added
- **Robustness**: Comprehensive error handling and null-byte sanitization to prevent database writer crashes.

## [2.9.12] - 2025-12-12

### Fixed
- **Sensors**: Fixed duplicate sensor classes and unused variables.

## [2.9.11] - 2025-12-12

### Changed
- **Sensors**: Improved adaptive units logic and split ratio sensors.
- **Writer**: Uses hypertable stats for more accurate compressed size reporting.

## [2.9.0] - 2025-12-11

### Added
- **Entity Globs**: Added support for `include_entity_globs` and `exclude_entity_globs` to filter entities by pattern.

## [2.8.4] - 2025-11-27

### Changed
- **Performance**: Increased default `batch_size` from 100 to 500. This reduces database transaction overhead on high-traffic systems without impacting latency on low-traffic systems (thanks to `flush_interval`).

## [2.8.3] - 2025-11-27

### Changed
- **Configuration**: Changed default `compress_after` from "60 days" to "7 days" to align with the default chunk interval. This ensures chunks are compressed as soon as they are full.

## [2.8.2] - 2025-11-27

### Fixed
- **Writer**: Fixed `AttributeError` in `init_db` causing initialization failure due to incomplete rename of configuration options.

## [2.8.1] - 2025-11-27

### Changed
- **Configuration**: Renamed `enable_*` configuration options to `enable_table_*` for clarity.
  - `enable_areas` -> `enable_table_areas`
  - `enable_devices` -> `enable_table_devices`
  - `enable_entities` -> `enable_table_entities`
  - `enable_integrations` -> `enable_table_integrations`
  - `enable_users` -> `enable_table_users`

## [2.8.0] - 2025-11-27

### Added
- **Configurable Metadata Tables**: Added YAML configuration options to enable/disable specific metadata tables.
  - `enable_areas` (default: true)
  - `enable_devices` (default: true)
  - `enable_entities` (default: true)
  - `enable_integrations` (default: true)
  - `enable_users` (default: true)
  - Disabling a table prevents it from being created and stops synchronization for that metadata type.

## [2.7.0] - 2025-11-27

### Added
- **Query Service**: Added `scribe.query` service to execute read-only SQL queries (`SELECT` only) directly from Home Assistant.
  - Useful for debugging and creating advanced sensors via automation.
  - Returns results as a list of dictionaries.

## [2.6.1] - 2025-11-27

### Added
- **Real-time Metadata Sync**: Scribe now listens to Home Assistant registry events to update `users`, `entities`, `devices`, and `areas` in real-time.
  - **Users**: Updates when users are added, updated, or removed.
  - **Entities**: Updates when entities are created or updated.
  - **Devices**: Updates when devices are created or updated.
  - **Areas**: Updates when areas are created or updated.

## [2.6.0] - 2025-11-26

### Added
- **Metadata Synchronization**: Scribe now syncs `areas`, `devices`, and `integrations` (config entries) to the database on startup.
  - **Areas Table**: Stores area ID, name, and picture.
  - **Devices Table**: Stores device ID, name, model, manufacturer, software version, area ID, and primary config entry.
  - **Integrations Table**: Stores config entry ID, domain, title, state, and source.
- **Comprehensive Metadata**: With `users` and `entities` already synced, Scribe now provides a full picture of the Home Assistant environment in TimescaleDB.

### Fixed
- **Test Suite**: Added comprehensive tests for metadata synchronization logic.

## [2.5.0] - 2025-11-267

### Added
- **Entities Table**: Added `entities` table and automatic syncing of Home Assistant entities (including scripts and automations) to the database. This allows joining `entity_id` with friendly names and other metadata.

## [2.4.0] - 2025-11-27

### Added
- **Users Table**: Added `users` table and automatic syncing of Home Assistant users to the database. This allows joining `context_user_id` with user metadata.
- **Logos**: Updated brand assets with new logos.

### Optimized
- **State Storage**: Optimized `states` table storage. If a state value is a valid float, the `state` column (string) is now set to `NULL` to save space. It is only populated if the value is non-numeric.

### Fixed
- **Users Sync**: Fixed `AttributeError` when syncing users. Now correctly uses `async_get_users()` to fetch user data.

## [2.2.23] - 2025-11-27

### Changed
- **CI**: Removed dev/bleeding edge tests from CI to improve stability.

## [2.2.22] - 2025-11-27

### Changed
- **Logging**: Changed debug logs to DEBUG level and cleaned up tests workflow.

## [2.2.21] - 2025-11-27

### Added
- **Debugging**: Added debug logging to inspect Home Assistant events.

## [2.2.7] - 2025-11-27

### Changed
- **Documentation**: Updated README and LICENSE (removed example column, added full config, added technical data link).

## [2.2.6] - 2025-11-27

### Changed
- **Documentation**: Updated README icons to Home Assistant blue.

## [2.2.5] - 2025-11-27

### Fixed
- **Documentation**: Fixed README icons display.

## [2.2.4] - 2025-11-27

### Added
- **SSL**: Implemented relative path support for SSL certificates.

## [2.2.2] - 2025-11-27

### Fixed
- **Tests**: Fixed `test_sensor` unit mismatch and models deprecation warning.

## [2.2.1] - 2025-11-27

### Fixed
- **Code Quality**: Fixed lint errors.
- **Documentation**: Updated README.
- **Sensors**: Changed duration unit to ms.

## [2.2.0] - 2025-11-27

### Optimized
- **Writer**: Optimized `ScribeWriter` using `deque`, `asyncio.gather`, and improved logging.

## [2.1.2] - 2025-11-27

### Changed
- **Documentation**: Refined README structure and added configuration tables.

## [2.0.0] - 2025-11-27

### Major Release
- **Production Ready**: Scribe 2.0.0 - Production-ready TimescaleDB integration.

## [1.11.0] - 2025-11-26

### Added
- **Retry Logic**: Implemented a robust retry mechanism for database writes. If the database is unreachable, events are buffered in memory (up to `max_queue_size`) and retried later.
- **Attribute Exclusion**: Added `exclude_attributes` configuration option (YAML and UI) to filter out specific attributes from being recorded.
- **Query Service**: Added `scribe.query` service to execute read-only SQL queries from Home Assistant.
- **Documentation**: Added comprehensive `TECHNICAL_DOCS.md` and updated `README.md` with "Scribe vs Recorder" comparison and troubleshooting guide.
- **Issue Templates**: Added GitHub issue templates for bug reports and feature requests.

### Changed
- **Scripts**: Generalized `deploy.sh` and `drop_db.sh` for public use (removed hardcoded paths).
- **Defaults**: Harmonized default values between UI and YAML configuration.
- **Logging**: Improved logging for connection errors and buffer status.

### Fixed
- **Sensors**: Resolved `AttributeError` in sensor initialization.

## [1.10.0] - 2025-11-25

### Added
- **Config Flow**: Enhanced configuration flow to split database URL into individual fields (Host, Port, User, Password, DB Name).
- **Auto-Creation**: Added logic to automatically create the target database if it doesn't exist.
- **Translations**: Added translations for new configuration fields.

## [1.9.0] - 2025-11-25

### Added
- **Statistics**: Implemented `ScribeDataUpdateCoordinator` to fetch database statistics (size, compression ratio) every 30 minutes.
- **Sensors**: Added sensors for database size and compression stats.

## [1.8.0] - 2025-11-25

### Added
- **Sensors**: Added `sensor.scribe_events_written`, `sensor.scribe_buffer_size`, `sensor.scribe_write_duration`.
- **Binary Sensor**: Added `binary_sensor.scribe_database_connection`.
- **Service**: Added `scribe.flush` service to manually trigger a write.

## [1.7.0] - 2025-11-25

### Changed
- **Config**: Restricted configuration to YAML only for advanced setups.

## [1.6.0] - 2025-11-25

### Added
- **Tests**: Added thorough test suite.
- **Features**: Advanced features integration.

## [1.5.1] - 2025-11-25

### Fixed
- **Code Quality**: Fixed indentation error in manifest.

## [1.5.0] - 2025-11-25

### Fixed
- **Crash**: Fixed initialization crash.

### Added
- **Localization**: Added more French translations.

## [1.4.0] - 2025-11-25

### Fixed
- **Bugs**: Various minor bug fixes.

### Added
- **Localization**: Initial French support.

## [1.3.1] - 2025-11-25

### Fixed
- **Config Flow**: Fixed configuration flow loop.
- **UI**: Added brand icons.

## [1.3.0] - 2025-11-25

### Added
- **Debug**: Added debug mode for enabling verbose logs.

## [1.2.0] - 2025-11-25

### Added
- **Schema**: Support for dynamic table creation.
- **Validation**: Input validation for configuration.

## [1.1.0] - 2025-11-25

### Added
- **Configuration**: Full YAML configuration support.

## [1.0.0] - 2025-11-25

### Initial Release
- Basic recording of states and events to TimescaleDB.
- Hypertables and Compression support.
- YAML and UI configuration.
