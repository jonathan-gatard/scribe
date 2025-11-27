# Changelog

All notable changes to this project will be documented in this file.

## [2.3.0] - 2025-11-27

### Added
- **Users Table**: Added `users` table and automatic syncing of Home Assistant users to the database. This allows joining `context_user_id` with user metadata.
- **Logos**: Updated brand assets with new logos.

### Optimized
- **State Storage**: Optimized `states` table storage. If a state value is a valid float, the `state` column (string) is now set to `NULL` to save space. It is only populated if the value is non-numeric.
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

## [1.0.0] - 2025-11-25

### Initial Release
- Basic recording of states and events to TimescaleDB.
- Hypertables and Compression support.
- YAML and UI configuration.
