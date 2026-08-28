# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- **A null byte in an attribute *key* stopped recording for good**: PostgreSQL refuses `\u0000` in a jsonb key exactly as it does in a value. Values were cleaned before reaching the codec — entity_id, state, event fields, attribute values — but the keys of an attributes mapping went through untouched, and a key is entirely under the control of whichever integration produced the state. One such key failed the `COPY`, which fails the whole batch, which is re-buffered and fails again on every retry: the same permanent stall as the duplicate timestamps fixed in 3.8, reached from the other end of the same document. Keys that are not strings are coerced too, since `json.dumps` refuses a tuple key outright with the same outcome.
- **System health described the integration, not the database**: "connected" was true whenever Scribe was loaded at all, so the panel read healthy while the database was unreachable and nothing was being recorded, and the version came from a key nothing ever set, so it always showed "Unknown". Both now report what they claim to, alongside the database Scribe is pointed at, whether TimescaleDB was found and how many items are buffered.
- **`scribe.query` returned results Home Assistant could not serialize**: `numeric` arrives from PostgreSQL as a `Decimal` — from `EXTRACT(EPOCH …)`, `avg()`, any `::numeric` — and `interval` as a `timedelta`. Selecting either gave the caller an obscure serialization error rather than the rows, though the query itself had run fine. Both are numbers now, so a template can use them.
- **A failure to register an entity silently dropped its states**: `_ensure_metadata_ids` logged its errors and returned, after which every state of an unregistered entity was skipped as "unknown entity_id" while the flush reported success — the queue was cleared and those states were gone. It hits hardest exactly where it hurts most, on a cold cache after a restart where every entity is unregistered. Resolving an entity is part of writing its state, so the error now fails the flush and the batch is re-buffered.
- **A database that was not up yet at boot cost the whole session**: Home Assistant and the database usually start together, and the database is often slower. When the pool could not be created, the writer raised its Repairs issue and gave up — no task was started, so `enqueue` dropped every state, and `buffer_on_failure` could not help because the queue was never fed. Scribe stayed dead until the next Home Assistant restart, *including* after the database came back. Connecting is now separate from starting: the loop reconnects on a backoff that doubles from 5s to a 5-minute ceiling, and everything recorded meanwhile is buffered and written when the database answers.
- **`db_url` was the one setting a YAML edit could not change**: it was read from the config entry before `configuration.yaml`, and the entry keeps the URL it was created with — so editing the line did nothing, and moving Scribe to another database meant deleting the integration and setting it up again. It now follows the same precedence as every other setting.

### Added
- **Configurable schema (#53)**: a new `db_schema` option puts everything Scribe creates — its tables, the `states` view, its hypertables and its compression and retention policies — into a PostgreSQL schema of your choosing instead of whatever the connection already points at. It is empty by default, which keeps the existing behaviour exactly, and is set in YAML or in the UI under *Advanced (TimescaleDB & SSL)*. This is what makes Scribe share a database cleanly: with its own schema it can sit next to your own transformed copies of the history, another integration's tables, or a second Home Assistant writing to the same server, each side independent down to its retention policy. The schema is created if the database user may create it, and an existing one only needs `USAGE` and `CREATE`. The name goes first on the connection's `search_path` — as a startup parameter rather than a `SET`, since asyncpg resets a pooled connection on release and would silently drop it after the first use — with `public` kept behind it, where the TimescaleDB extension installs the functions Scribe has to call. A schema that cannot be reached **stops recording** rather than falling back: PostgreSQL does not fail on a missing schema, it quietly moves to the next entry on the path, so a typo or a missing grant would otherwise fill `public` while the UI showed something else — Scribe checks where its session actually landed and raises a self-clearing Repairs issue naming both schemas and the grants to give. Every catalog lookup is now scoped to the schema in use, and the statements that *drop* things are name-qualified, so two Scribe schemas in one database cannot read each other's chunk statistics or delete each other's `states` view — which also fixes legacy-schema detection for anyone who reached the same result before this by putting `?options=-csearch_path%3D…` in their URL, where another installation's `public.states` would block startup. Requested by @cuthulino. Documented in a new *Database schema* section in all four READMEs, translated into English, French, Spanish and German, and covered by unit tests plus end-to-end tests that record into a non-`public` schema against a real TimescaleDB and check what landed where.
- **The Repairs panel is translated into Spanish and German**: the README exists in four languages while the integration's own strings existed in two, so a Spanish or German user read all fourteen issues in English — the texts someone reads precisely when something has gone wrong. `es.json` and `de.json` now match `strings.json` key for key, with tests keeping every documented language complete, forbidding a translation from inventing a key of its own, and checking that each `{placeholder}` survives translation.
- **A TLS setup that could not be fully applied is reported**: a configured client certificate that is missing, or whose key does not match, was logged and stepped over, and the connection went ahead without client authentication. It is encrypted either way and looks exactly like a working mutual-TLS setup from the outside — which is the problem, since the part that fails is the part nobody notices until an audit. A self-clearing Repairs issue now names each problem.
- **Diagnostics report what the writer is doing**: the download carried only the config entry, which says what was asked for and nothing about what is happening. It now includes the connection state, whether TimescaleDB was found, the queue and what it has dropped, consecutive flush failures, the counters, the storage and retention settings, and how many entities are cached. The database URL never appears, and `last_error` goes through the same DSN redaction as the connectivity entity.

### Changed
- **`states_raw` no longer carries an index its primary key already provides**: the key on `(metadata_id, time)` answers `WHERE metadata_id = x ORDER BY time DESC` on its own, since a B-tree is scanned in either direction. The extra index on `(metadata_id, time DESC)` served no query the key did not, while costing 21% of every write and exactly doubling the index footprint of the largest table (measured on two identical databases: 42.6 ms against 51.4 ms per 5000-row batch, 2.1 MB against 4.2 MB). New installations no longer create it, existing ones have it dropped at startup.
- **The `states` view stopped materializing the whole entities row**: it drove its lateral join from a CTE selecting `*`, which carried every entity's `capabilities` jsonb — usually the largest column there — through every query, while the view projects two columns. On 800 entities and 400 000 states: the last ten states of one entity went from 5.48 ms to 3.52 ms, and a day grouped by entity from 66.07 ms to 45.74 ms.
- **The startup row counter reads the hypertable, not the view over it** (40 ms against 173 ms over 400 000 rows), and the compression figures are fetched once per table instead of twice — the second call re-ran an aggregate over every chunk to obtain a value already in hand.
- **The rows written by `COPY` are built while resolving, not twice**: resolution produced dicts that the write path then walked again to build tuples. Measured over 5000 states, the whole path from resolution to the tuples `COPY` receives went from 2.95 ms (3.8's behaviour, which mutated the queued items) to 1.95 ms, while keeping the items read-only so a re-buffered batch still resolves on the retry.
- **Test coverage from 91% to 95%**, with the config flow and diagnostics at 100%: the TLS path, the config flow, the registry listeners and services, the attribute sanitizer and the TimescaleDB gate had no tests of their own. Two of the bugs above were found writing them.

## [4.0.0] - 2026-08-23

> **Breaking changes.** Two things can stop Scribe from starting on an
> installation where it used to run, both deliberate and both explained in the
> Repairs panel rather than only in the log:
>
> - **A database older than Scribe 3.0 is no longer converted.** Scribe stops
>   without renaming, creating or deleting anything, and points at 3.8 — the
>   last version able to do the conversion. Install it, let the migration
>   finish, then update again. Databases created by any 3.x release are
>   unaffected.
> - **TimescaleDB is required for new installations.** Scribe enables the
>   extension itself when it can; when it cannot, setup is refused instead of
>   silently producing an install where chunking, compression and retention do
>   nothing. **Installations already running without it keep recording** and are
>   not cut off.


### Added
- **README translated into French, Spanish and German**, with a flag bar at the top of each file to switch between them (`README.fr.md`, `README.es.md`, `README.de.md`). Configuration keys, SQL, sensor names and YAML stay untouched — only prose and code comments are translated — and the four files are checked to share the same structure, so a section added to one is visibly missing from the others.
- **Four more conditions surfaced in Repairs instead of the log**: Scribe reports what it cannot do rather than leaving it in a log file nobody reads on a good day. *Could not create its tables* — the database answers but the schema could not be built (almost always privileges): on a new database nothing is recorded at all, on an existing one recording may be partial. *Could not create the `states` view* — states keep being written to `states_raw`, but the view every query, dashboard and documentation example goes through is missing, so the history looks empty when nothing is lost. *`{table}` is not a hypertable* — TimescaleDB is installed but the table was never converted, so chunking, compression and retention silently do nothing; this is the common outcome when the extension is installed *after* the tables filled up, since `create_hypertable` refuses a non-empty table. *`{table}` is never compressed* — the table is a hypertable but carries no compression policy, and keeps its full uncompressed size. The last two are checked against what the database actually ended up with rather than against the errors raised on the way, and are only raised when TimescaleDB is installed — on plain PostgreSQL the existing *TimescaleDB is not installed* issue already says it once, and the hypertable steps are now skipped entirely instead of failing into the log at every start. All four retire themselves. Translated in English and French, documented in the README's Troubleshooting table.
- **Configurable retention (#53)**: two new options, `retention_states` and `retention_events`, hand a TimescaleDB retention policy to Scribe's hypertables — history older than the interval you set is dropped, chunk by chunk. Both are empty by default, which is the behaviour Scribe has always had: nothing is ever deleted unless you ask. They are configurable in YAML and in the UI under *Advanced (TimescaleDB & SSL)*, states and events separately, so a noisy event stream can be expired while state history is kept. Scribe owns the policy on its own tables: changing the value updates it, clearing the field removes it — otherwise the UI would offer no way to stop deletions it had started. An unchanged value is left alone rather than re-created, since re-creating the job would postpone its next run on every Home Assistant restart. Values are validated as plain intervals (`30 days`, `6 months`, `1 year`) before reaching SQL, and a policy that cannot be applied — no TimescaleDB, not a hypertable, insufficient privileges — raises a self-clearing Repairs issue instead of leaving the user to believe their database is bounded when it is not. Removing the setting always means "keep forever": clearing the UI field and deleting the line from `configuration.yaml` both drop the policy, because a YAML import copies its keys into the config entry and nothing removes them when the line disappears — retention is read past that copy so a value can never outlive the line that asked for it. Requested by @cuthulino. Documented in the README's new *Retention* section, translated in English and French, and covered by unit tests plus end-to-end tests that run the policy against a real TimescaleDB and check what survives it.

### Removed
- **`models.py`**, a leftover set of SQLAlchemy table definitions. Nothing imported it but its own test, it declared a `states` table with a shape that stopped existing in 3.0, and it pulled in SQLAlchemy — which is not in `manifest.json`. It shipped in the HACS payload for nothing.

### Changed
- **`ScribeWriter` now takes a `WriterConfig` instead of twenty-three parameters.** Every default lives in one dataclass rather than being repeated at each call site, and a misplaced argument is no longer easy to write and hard to see. Setup resolves one of these and hands it over.
- **`async_setup_entry` split into named pieces**: it was 840 lines holding eleven nested functions, and SonarQube measured its cognitive complexity at **199** against a limit of 15 — nesting is what costs, so the same branches were being charged four and five times over. The registry sync, the state and event listeners, the registry listeners, the coordinators and the services now live at module level, and the resolution of settings moved into `_resolve_settings`. Setup is 175 lines with a complexity of **9**, no behaviour changed, and one round of duplication went with it: the row for each registry (`_entity_row`, `_area_row`, `_device_row`, `_user_row`, `_integration_row`) was built once for the startup sync and again for the live listener, and is now built in one place.
- **The three backfill scripts** (`influx2scribe`, `ltss2scribe`, `recorder2scribe`) had the same shape: one `migrate()` doing connection, preflight, purge and the whole chunk loop, at complexity 66/32/32. Each now reads its chunk and inserts it through named helpers.
- **`writer.py` split into named steps**: `_flush`, `init_db`, `rename_entity`, `write_entities`, `get_db_stats` and `_sanitize_obj` were long enough that CodeFactor graded the file B on ten complexity findings. Each is now a short body calling helpers that say what they do — `_split_batch`, `_resolve_state_metadata_ids`, `_copy_batch`, `_record_flush_success`, `_handle_flush_failure`, `_create_tables`, `_init_hypertables`, `_take_over_occupied_name`, `_partition_entities`, `_sanitize_scalar` — with no behaviour change, and the two flush bugs above were found while separating the paths. All ten findings are gone.
- **The analysis configuration is in the repository** (`sonar-project.properties`), including the five rules that are silenced with the reason each one does not fit — `logging.exception()` where `exc_info=True` is already passed, async Home Assistant hooks that never await, the example connection URLs in the translated UI strings, `\w` in an SQL identifier allowlist (it matches Unicode, which is the opposite of what that check is for), and the deliberate `CancelledError` in `writer.stop()`. Everything else was fixed: SonarQube reports **0 bugs, 0 vulnerabilities, 0 code smells, no technical debt**, all ratings A.
- **The README no longer claims 100% test coverage**, which was never measured: it is ~90% of lines (72.8% counting branches, as SonarQube does). It now says that, and says the thing that actually matters — that the end-to-end suite runs against a real TimescaleDB rather than mocks.

- **TimescaleDB is now required for new installations, and Scribe enables it for you**: chunking, compression, retention and every size sensor are the reason Scribe exists, and none of them work on plain PostgreSQL — where the database also grows several times faster. Until now a new install could land there silently and only find out months later. Setup now checks, in the UI flow and on a first YAML import alike, and refuses with an explanation naming the statement to run. Before refusing it *tries to fix it*: if the server has the extension available and the Scribe database user holds `CREATE` on the database — which the documented setup grants — Scribe runs `CREATE EXTENSION timescaledb` itself, so the most common cause (a forgotten line during setup) resolves without anyone reading a log. A refused YAML import leaves no config entry behind, so granting the right and restarting is enough. **Installations that already run on plain PostgreSQL are not cut off**: they are never re-checked, they keep recording, and the existing *TimescaleDB is not installed* Repairs issue keeps telling them what they are missing — stopping their recording at an update would be a far worse outcome than a database that grows too fast. Verified end to end against a real Home Assistant.

### Fixed
- **A failed batch was only re-buffered on one of the two failure paths**: a client-side error (the connection dropping) re-buffered the batch, while a *server-side* `PostgresError` — a full disk, a revoked grant, a statement timeout, exactly what buffering exists for — logged "Buffering N items" and then dropped every one of them. The re-buffering line lived in one handler and not the other. Both now go through a single failure path, which also means a server-side error with buffering disabled finally counts its dropped records and raises the *Discarding records* issue instead of losing them silently.
- **A buffered batch lost all of its states on the retry**: resolving `metadata_id`s popped `entity_id` off the batch items in place. When the write then failed, those same items were re-buffered without the key they are resolved by, so the next attempt skipped every one as "unknown entity" — the batch was kept, then quietly emptied. The resolution no longer mutates what it reads.
- **A flush task could be garbage-collected mid-write**: `asyncio.create_task` was called fire-and-forget, and the event loop only holds weak references to tasks. A batch already drained out of the queue could therefore vanish with nothing raised anywhere. Flush tasks are now held until they finish. Found by SonarQube (`python:S7502`).
- **The writer task reported itself as completed after being cancelled**: its loop caught `asyncio.CancelledError` and `break`, so the coroutine returned normally and `stop()` never saw the cancellation it had just requested. It is re-raised now (`python:S7497`).
- **`chunk_time_interval` and `compress_after` were ignored after the first start**: both are applied through `if_not_exists => TRUE` — `create_hypertable` for the chunk size, `add_compression_policy` for the compression interval — and that flag makes TimescaleDB skip the call entirely when the object already exists, *including* when the arguments differ. Changing either value in the UI or in `configuration.yaml` therefore did nothing on any database past its first start, while the log announced the new value (`Converting states_raw to hypertable (chunk=7 days)…` against a table still cut into 1-day chunks). Scribe now compares what the database actually has and reconciles it: `set_chunk_time_interval` when the chunk size differs, and a replaced policy when the compression interval does. Neither costs data — chunks already written keep the span they were created with (they are never rewritten or moved) and chunks already compressed stay compressed, since the policy only decides when the *next* ones are. An unchanged value is left strictly alone, so restarts cause no churn. Verified end to end against a real TimescaleDB, and the README gained a *Storage tuning* section explaining what chunks are and how these settings interact with retention.

### Removed
- **The 2.x → 3.x database migration**: converting a pre-3.0 schema — renaming `states`, backfilling `states_raw` in 12-hour chunks, rebuilding the `entities` primary key, adding constraints on a compressed hypertable — has been carried in every 3.x release since February. It ran for a shrinking population (Scribe entered the HACS default store on 2026-08-01, so nearly every current install started on 3.7 or later) while charging everyone for it: `migrate_database` slept **60 seconds** on every start where `states_raw` did not exist, which for `record_states: false` meant every start, forever. `migration.py` is gone.

  Scribe now *detects* the old layout instead of converting it. A `states` base table, a leftover `states_legacy`, or an `entities` table without an `id` column stops the writer before it creates anything: nothing is renamed, created or deleted, so the database stays exactly as it was and **Scribe 3.8 can still convert it**. A Repairs issue (English and French) says precisely that — install 3.8, let the migration finish, update again — and the writer stops queueing states it could never write rather than filling the buffer. Documented in the README's new *Upgrading from Scribe 2.x* section.

  Two more pieces went with it. `_convert_to_hypertable` duplicated `_init_hypertable`, which already runs on every start. `_migrate_events_pk` added an `id BIGSERIAL PRIMARY KEY` to `events` on every TimescaleDB-less install — a full table rewrite under an `ACCESS EXCLUSIVE` lock, for a column nothing in Scribe has ever read.

  The `fk_states_raw_entity` foreign key was left as it is: only migrated databases have it, since 3.6.0 creates `states_raw` without it. Adding it to fresh installs would put a per-row referential check on the `COPY` write path to enforce something `_ensure_metadata_ids` already guarantees, so the two shapes are allowed to converge on their own.

## [3.8.0] - 2026-08-13

### Added
- **Repairs issues for the failures that used to be log-only**: Scribe now surfaces six more conditions in Home Assistant's Repairs dashboard, each explaining the consequence and the fix, and each retiring itself automatically once resolved — an unreachable database (nothing is being recorded), repeated write failures (data held in memory, lost on restart), a saturated buffer (history being discarded), records dropped because buffering is disabled, a missing TimescaleDB extension (no chunking or compression, database grows much faster, size sensors stay empty), and a failed legacy migration (old history stranded in `states_legacy`). Write failures only raise an issue after three consecutive flushes fail, so a database restart or a brief network drop stays silent. Translated in English and French, and documented in the README's Troubleshooting section.

### Fixed
- **`scribe.query` could not be stopped**: the service hands arbitrary SQL to the database on a pooled connection with no time limit, so a single careless aggregate over a large hypertable pinned that connection and worked the server until it finished — starving the writer and, on a memory-tight machine, dragging it into swap. Queries now run under a 120-second `statement_timeout`, scoped to their own transaction.
- **Every Home Assistant start scanned the whole history**: the initial "states written" counter ran `SELECT count(*)`, which aggregates every row across every chunk and decompresses each one. On a year-old install that is tens of millions of rows — measured at 103 chunks on the author's database — at every restart. Those two counters exist only to feed the I/O statistics sensors, which are opt-in and off by default, so they are no longer computed unless those sensors are enabled. A default install now pays nothing at all. TimescaleDB's `approximate_row_count()` was tried and rejected: it derives compressed chunks from `reltuples`, which counts batches and assumes each is full — measured at 1 270 000 against 444 968 actual on a real chunk, 2.85x too high. A counter that overstates by nearly three times is worse than a slow one, so the count stays exact.
- **Raw driver text was exposed in a state attribute**: the connectivity entity published `last_error` verbatim. State attributes are readable by every Home Assistant user and are written to the recorder, the logbook and the history, so any connection string appearing in a driver message would have been persisted in all three. Current asyncpg messages do not carry one; connection strings are now stripped regardless.
- **Options set in the UI were ignored whenever `configuration.yaml` had a `scribe:` block (#52)**: Home Assistant validates that block against Scribe's schema before setup, and the validation filled in *every* optional key with its default. A YAML file containing nothing but `db_url` therefore reached Scribe as a config that also declared `enable_stats_io: false`, empty filter lists, and so on — and YAML outranks the options flow. Settings were saved and redisplayed as enabled, but never applied: the reporter had all three statistics toggles on and only `binary_sensor.scribe_database_connection` created. The schema no longer injects defaults, so keys you did not write fall through to your UI options; YAML still wins for keys you actually set. Reported by @shaver.
- **Recording could stall permanently on duplicate timestamps**: two states for the same entity at the same instant — Home Assistant emits them when a restored state meets a live one, or on `force_update` — violated `states_raw`'s `(metadata_id, time)` primary key. `COPY` has no `ON CONFLICT` clause, so the **whole batch** failed, was re-buffered, and failed again on every retry: the queue grew to its cap and Scribe stopped recording until restart. Batches are now de-duplicated on that key before the write (last state wins), and a batch colliding with rows already stored falls back to `ON CONFLICT DO NOTHING` instead of aborting.
- **Flush crash on `date` attributes**: an entity exposing a plain `datetime.date` in its attributes (expiry dates, calendar entities, `input_datetime` helpers in date mode) raised `Object of type date is not JSON serializable` inside the jsonb codec, killing the **entire flush batch** — every state and event in it lost or endlessly re-buffered. Home Assistant's `JSONEncoder` handles `datetime` but not a bare `date`; the 3.6.1 fix for #40 addressed the `time` column and left this path open. Dates are now serialized as ISO strings.

### Changed
- **End-to-end test suite covering the whole component** (`tests/integration/`): 119 tests driving the real integration against a real TimescaleDB and a real Home Assistant instance. They cover schema and hypertable creation, the flush and sanitization path, metadata sync for all five registries, statistics, the read-only query service, legacy `states` → `states_raw` migration, entity renames, behaviour against **compressed chunks**, failure and load (database loss and recovery, buffer cap, 5000-state batches, concurrent flushes), and the full Home Assistant lifecycle — config flow against a real database, real state changes through the real filters, services, options reload, diagnostics redaction. They assert on what actually lands in the tables rather than on mocked SQL calls, which is how most of the fixes above were found. CI runs them against a service container and fails if they skip.
- **Two redundant statistics queries removed**: `get_states_compression_stats` and `get_events_compression_stats` recomputed keys their `*_size_stats` counterparts already produce, and the states one queried the `states` *view* instead of the `states_raw` hypertable, so it could never return anything. Each statistics refresh now makes two fewer round-trips.

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
