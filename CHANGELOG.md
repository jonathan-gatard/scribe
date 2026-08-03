# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- **Flush crash on `date` attributes**: an entity exposing a plain `datetime.date` in its attributes (expiry dates, calendar entities, `input_datetime` helpers in date mode) raised `Object of type date is not JSON serializable` inside the jsonb codec, killing the **entire flush batch** — every state and event in it was lost or endlessly re-buffered. Home Assistant's `JSONEncoder` handles `datetime` but not a bare `date`; the 3.6.1 fix for #40 addressed the `time` column and left this path open. Dates are now serialized as ISO strings. Found by the new end-to-end test suite.

### Changed
- **End-to-end test suite covering the whole component** (`tests/integration/`): 54 tests driving the real writer against a real TimescaleDB and a real Home Assistant registry — schema and hypertable creation, the flush and sanitization path, metadata sync for all five registries, statistics, the read-only query service, legacy `states` → `states_raw` migration, and entity renames. They assert on what actually lands in the tables rather than on mocked SQL calls. CI runs them against a service container and fails if they skip.

## [3.7.0] - 2026-08-03

### Added
- **Repairs issue when a rename cannot be applied**: a refused rename (destination occupied by a live or unprovable entity) or a failed one (database error) used to be visible only in the logs, while the entity's history silently split across two IDs. Scribe now raises a Home Assistant Repairs issue explaining what happened and what to do; a later successful rename to the same destination retires it automatically. Translated in English and French.
- **`lovelace_scribe_card.yaml`**: the dashboard as a single `vertical-stack` card, pasteable into "Add card" → "Manual". Unlike the view variant it works in any view type, including Sections views.

### Fixed
- **Entity renames no longer silently lost on collision**: when an entity was renamed to an entity_id that already existed in the `entities` table (typically a stale row left behind by a device that was removed and re-added), `rename_entity` hit a `UniqueViolationError`, logged a warning and gave up — the entity kept writing under its old name and its history split. Scribe now checks whether the occupant still resolves in Home Assistant's live entity registry (via `unique_id`/`domain`/`platform`). If it is provably dead, the orphan row is reused: its `states_raw` history is merged into the renamed entity's `metadata_id` and its metadata row deleted, yielding one continuous history under the new name. If the occupant is still alive — or its registry coordinates are incomplete, making death unprovable — the rename is refused and nothing is modified. Covered by a new test suite (`tests/test_rename_entity.py`).
- **Rename racing against metadata sync (found during 3.7.0b1 testing)**: Home Assistant fires registry events as concurrent tasks, so a metadata-sync task could insert the *destination* row (same entity, full metadata) while the rename was in flight — the rename then collided with the entity's own fresh row and was refused as a "live occupant". Metadata writes (`rename_entity`, `write_entities`, and the flush section that resolves and uses `metadata_id`s) are now serialized behind a single lock, and a self-collision is recognized and merged instead of refused — every row involved belongs to the same entity, so the merge is unconditionally safe and also heals rows desynced by older versions. A self-collision is identified by the two rows carrying the same `unique_id`: the live registry cannot distinguish it from a different entity legitimately occupying the destination, since in both cases the occupant resolves to the destination entity_id.
- **History merge aborting on duplicate timestamps**: `states_raw`'s primary key is `(metadata_id, time)`, so merging an occupant's history into the surviving entity failed with a `UniqueViolationError` wherever both had recorded a state at the same instant — rolling back the entire rename. Colliding occupant rows are now dropped first (the surviving entity's own row wins) and the count is logged. Found by the new end-to-end test suite.
- **"No card type configured" when adding the dashboard (#45)**: the README said to click "the **+** button to add a new View", but in current Home Assistant that button is *Add card*. `lovelace_scribe_view.yaml` is a view config (`title` / `icon` / `cards`), which a card editor rejects because it has no root `type:` key. The dashboard section now documents the card and the view variants separately, with the correct UI path for each. Reported by @shaver.

### Changed
- **End-to-end tests against a real TimescaleDB**: `tests/test_rename_integration.py` drives the real writer, a real database and a real Home Assistant entity registry, asserting on what actually lands in the tables rather than on mocked SQL calls. CI runs it against a TimescaleDB service container and fails if those tests skip. Both rename bugs above were found this way.
- **Releases are now gated by CI**: pushing a `v*` tag runs the full test suite before the GitHub release is created, and the workflow fails if `manifest.json`'s version doesn't match the tag. Tags ending in `aN`/`bN`/`rcN` (e.g. `v3.7.0b1`) are published as GitHub pre-releases, which HACS only offers to users who opted into beta versions.

## [3.6.2] - 2026-05-13

### Fixed
- **Invalid-config error on removed `enable_table_entities` option (regression from 3.6.0)**: 3.6.0 removed the option from the schema and the CHANGELOG promised unknown keys would be ignored, but `extra=vol.ALLOW_EXTRA` was only set on the outer (top-level) schema, not on the inner `scribe:` block. Existing YAML configs carrying the option now fail validation with `'enable_table_entities' is an invalid option for 'scribe'`. The inner schema now also allows extras, matching the documented behavior.

## [3.6.1] - 2026-05-13

### Fixed
- **Flush crash on datetime attributes (regression from 3.6.0)**: the `_sanitize_obj` recursion introduced in 3.6.0 walked every value of a batch item, including the `time` field. `datetime`/`date` instances did not match any of the early type guards and fell through to the `str(obj)` fallback, after which `asyncpg`'s `timestamptz_encode` rejected the ISO string with `TypeError: expected a datetime.date or datetime.datetime instance, got 'str'`. `_sanitize_obj` now returns `datetime`/`date` values unchanged. Reported and fixed in #40 by @jaal2001.
- **`UniqueViolationError` on concurrent entity inserts**: `write_entities` used a `SELECT`-then-`INSERT` pattern (to avoid `SERIAL` sequence bloat from `ON CONFLICT DO UPDATE`), but two concurrent registry-sync triggers could both observe the same entity as absent and race on the `INSERT`. Reproducible with intermittently-online devices like Chromecast switches that fire multiple registry syncs when they reappear. Added `ON CONFLICT (entity_id) DO NOTHING` as a safety net — unlike `DO UPDATE`, it doesn't advance the sequence on conflict. Fixed in #40 by @jaal2001.

## [3.6.0] - 2026-05-07

### Fixed
- **Flush crash on non-JSON-native attributes (#35)**: a single `TargetChannelInfo` (or any custom integration object) inside `attributes` made `json.dumps` raise inside the asyncpg jsonb codec, killing the whole flush batch with `Object of type X is not JSON serializable (TypeError)`. `_sanitize_obj` now converts non-JSON-native values before they reach the codec — dataclasses to dict (preserves field names), everything else (UUID, integration-specific objects, …) to string. Reported by @jaal2001, with an initial UUID-specific patch by @azebro (#37) kept as defense-in-depth.
- **`exclude_entity_globs` now overrides `include_entity_globs` (#33)**: Home Assistant's `generate_filter` lets `include_entity_globs` short-circuit over `exclude_entity_globs` (case 4a). Scribe now wraps the upstream filter so an exclude-glob match is always a hard reject. Contributed in #34 by @SAY-5.
- **Migration scripts no longer crash without the `states_raw` PK (#31)**: `influx2scribe`, `ltss2scribe` and `recorder2scribe` now run a schema preflight before any chunk. If the destination is missing the `entities` UNIQUE constraint or the `states_raw` PK on `(metadata_id, time)`, the script fails fast with guidance to start Scribe for ≥15 minutes so the background migration can complete. Reported by @frankvandenhurk.

### Changed
- **`entities` table is now always created**: removed the `enable_table_entities` configuration option. The table was already de-facto mandatory — every state insert upserts into it via `_ensure_metadata_ids` and the `states` view joins on it — so disabling it silently broke `record_states=True`. Existing YAML configs carrying the option are ignored (the schema accepts unknown keys).
- **PRIMARY KEY on `states_raw` at table creation**: the PK on `(metadata_id, time)` is now part of `CREATE TABLE` instead of being added by the post-bootstrap migration. New installs have the constraint from the first row, so `ON CONFLICT (metadata_id, time)` in migration scripts works immediately without waiting for the 60s + background-migration window.

### Added
- **Documentation**: `datastructre.md` — a guide on Scribe's database structure with query examples. Contributed in #36 by @jaal2001.

## [3.5.0] - 2026-04-24

### Added
- **Event filtering**: Two new configuration options, `include_events` and `exclude_events`, let you restrict which Home Assistant event types Scribe records. When `include_events` is set, Scribe subscribes only to the listed event types (rather than the bus-wide `MATCH_ALL` listener), which can noticeably reduce CPU on busy instances. `exclude_events` suppresses specific types and takes precedence over `include_events` on overlap. Both are configurable via YAML and the UI options flow. The `include_events` option was contributed in #30; `exclude_events` completes the symmetry with the other include/exclude filter pairs.

## [3.4.1] - 2026-04-19

### Changed
- **Logging**: Overhauled all warning/error log messages across `migration.py`, `writer.py`, `coordinator.py`, and `__init__.py` to prefix each message with `[module.function]` context, include the exception type, attach a full traceback via `exc_info=True`, and surface the operation parameters (table names, step name, SQLSTATE, batch sizes, etc.). Diagnosing migration and write failures from the Home Assistant logs is now significantly easier — errors pinpoint the exact call site and operation that failed.

## [3.4.0] - 2026-04-19

### Added
- **Performance**: Integrated native PostgreSQL `COPY` operations for massive batch insertion performance improvements, significantly lowering CPU and Memory footprint on Home Assistant thanks to [@hermes-agent]'s PR #28.

### Changed
- **Architectural Refactoring**: Core database driver (`asyncpg`) was rebuilt to natively handle Home Assistant `jsonb` data via native codecs. This prevents JSON double-encoding errors and `DataError` crashes while removing heavy Python string serialization (`json.dumps()`) across the system.

## [3.3.1] - 2026-04-18

### Fixed
- **Connection Failure Loops**: Fixed an architecture bug where the connection pool failing to initialize at startup would cause an infinite retry loop of empty async tasks, severely degrading Home Assistant CPU performance and flooding error logs. Scribe now correctly aborts queued operations cleanly when database connection fails at launch.

## [3.3.0] - 2026-04-18

### Fixed
- **Migration on fresh installs without TimescaleDB**: Fixed `InFailedSQLTransactionError` crash during migration. Querying `timescaledb_information.hypertables` inside a transaction would poison the entire PostgreSQL transaction when TimescaleDB was not installed.

### Changed
- **TimescaleDB detection**: TimescaleDB availability is now detected once at startup via `pg_extension` (always safe on any PostgreSQL instance) and passed as a flag to all migration functions. This replaces fragile per-query try/except blocks.
- **Hypertable conversion**: `_convert_to_hypertable` is now skipped entirely when TimescaleDB is not installed, avoiding unnecessary error logging.

## [3.2.4] - 2026-03-17

### Removed
- **release_notes.md**: Removed the redundant release notes file.

### Fixed
- **Code Quality**: Fixed unused import in `sensor.py`.

## [3.2.3] - 2026-03-17

### Fixed
- **YAML Configuration Priority**: All configuration options defined in `configuration.yaml` now correctly take priority over settings configured via the Home Assistant UI.
- **Statistics Sensors**: Fixed an issue where `enable_stats_io`, `enable_stats_chunk`, and `enable_stats_size` were ignored when provided via YAML.

## [3.2.2] - 2026-03-14

### Fixed
- **Migration on compressed hypertables**: The `states_raw` constraints migration now temporarily disables TimescaleDB compression to avoid errors during table alteration.
- **`states_legacy` cleanup**: Added `CASCADE` to `DROP TABLE states_legacy` to handle dependent TimescaleDB internal views.

## [3.2.1] - 2026-03-08

### Added
- **Global Translations**: Updated all translation files (`es`, `it`, `pt`, `nl`, `de`, `ru`, `sv`, `da`, `ja`, `pl`, `sk`) to support the new multi-step configuration UI.

### Fixed
- **Sensor Accuracy**: Refined size sensors to always return raw bytes, ensuring Home Assistant correctly handles adaptive unit scaling (kB/MB/GB/TB).

## [3.2.0] - 2026-03-08

### Added
- **UI Configuration**: The integration options can now be fully configured from the Home Assistant UI via organized steps (Recording, Performance, Stats, Metadata, Advanced).
- **Filtering**: Added support for entity lookup and glob filtering (`include_entity_globs` and `exclude_entity_globs`).
- **Security**: Strict validation for custom table names to prevent SQL injection.
- **Improved Sensors**: Statistics sensors now grouped by category (I/O, Chunk, Size) with configurable refresh intervals.

### Fixed (Performance)
- **`states` View**: Implemented a `MATERIALIZED` CTE for the entities lookup. This optimization ensures **TimescaleDB Segment Pruning** regardless of hardware type or PostgreSQL cost settings.
- **Native Scaling**: Size sensors now use Home Assistant's native `DATA_SIZE` device class for consistent unit display.

### Changed
- **Core Refactor**: Major codebase cleanup for better stability, error handling, and SSL/TLS reliability.
- **Translations**: Complete French and English localizations for all UI options.


## [3.0.0] - 2026-03-05

### Changed (Breaking Changes)
- **Database Schema**: Major database schema migration. The integration now uses a new `states_raw` underlying table with optimized primary and foreign keys for hypertable chunks. Legacy data from `states_legacy` is migrated in the background to prevent Home Assistant startup timeouts. 
- **Dependencies**: Removed dependency on `greenlet` by switching from SQLAlchemy to `asyncpg` directly. This restores compatibility with Python 3.14 and Alpine Linux (Home Assistant OS).

## [2.12.7] - 2026-01-11

### Fixed
- **UX**: Added `suggested_unit_of_measurement` to size sensors to force Home Assistant to respect the dynamic adaptive units display. This fixes issues where units were sticking to "B" despite the values being in MB/GB.

## [2.12.6] - 2026-01-11

### Changed
- **UX**: Implemented adaptive units for database size sensors.
  - **kB**: < 1 MB (0 decimals)
  - **MB**: < 1 GB (1 decimal)
  - **GB**: >= 1 GB (2 decimals)
  - Values automatically scale based on size for optimal readability.

## [2.12.5] - 2026-01-11

### Changed
- **UX**: Removed decimal precision from database size sensors (MB).
  - Values are now rounded to the nearest integer for cleaner display (e.g. `619 MB`).

## [2.12.4] - 2026-01-11

### Changed
- **UX**: Changed display unit for database size sensors from Bytes (B) to Megabytes (MB).
  - This improves readability for larger databases, avoiding long numbers like `649,256,960 B`.
  - Values are now rounded to 2 decimal places.

## [2.12.3] - 2026-01-11

### Fixed
- **Statistics**: Fixed `TypeError: unsupported operand type(s) for -: 'int' and 'NoneType'` in `get_db_stats`.
  - Ensure `total_bytes` and `compressed_bytes` default to 0 if the database returns None (e.g. empty tables or stats not yet available).
  - This prevents the integration from crashing during startup or stats update intervals.
  - Applied fix to both States and Events size statistics.

## [2.12.2] - 2026-01-11

### Security
- **Paranoid Sanitization**: Verified and enforced strict type sanitization (TEXT columns = string parameters) for ALL database insertions.
  - Enforced `str()` conversion and null byte removal for critical high-frequency fields in `states` and `events` tables (`entity_id`, `state`, `event_type`, `origin`, etc.).
  - This ensures 100% compliance with database types, even if upstream components provide incorrect types (e.g. int instead of string).

## [2.12.1] - 2026-01-11

### Fixed
- **Metadata Sync**: Fixed all metadata table insertion failures with comprehensive sanitization.
  - Added type conversion (`str()`) and null byte removal for all TEXT fields before DB insertion.
  - Affected methods: `write_users`, `write_entities`, `write_areas`, `write_devices`, `write_integrations`.
  - Fixes "expected str, got int" and "invalid byte sequence for encoding UTF8: 0x00" errors.

## [2.12.0] - 2026-01-11

### Changed
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
