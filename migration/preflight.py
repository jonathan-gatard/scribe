"""Preflight checks for Scribe migration scripts.

The migration scripts (`influx2scribe`, `ltss2scribe`, `recorder2scribe`)
all rely on:
  - the `entities` table with a UNIQUE on `entity_id`
    (used by `INSERT ... ON CONFLICT (entity_id) DO UPDATE`)
  - the `states_raw` table with a PRIMARY KEY on `(metadata_id, time)`
    (used by `INSERT ... ON CONFLICT (metadata_id, time) DO NOTHING`)

The Scribe component creates the tables at startup but the `states_raw`
PK is added by a background migration that runs ~60s after Home
Assistant boots. Running a migration script before that completes
produces cryptic per-chunk errors. This module fails fast with a clear
message instead.
"""

import logging
import sys

_GUIDANCE = (
    "👉 Start Scribe (in Home Assistant) and let it run for at least 15 "
    "minutes so the background schema migration completes, then re-run "
    "this script.\n"
    "   If the error persists, please open an issue and include the lines "
    "above:\n"
    "   https://github.com/jonathan-gtd/scribe/issues"
)


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def _as_column_list(agg) -> list[str]:
    """Normalize a Postgres ``array_agg`` result to a list of column names.

    psycopg2 usually adapts a Postgres array to a Python ``list``, but on some
    versions/platforms it returns the raw array literal (e.g. ``"{id,name}"``)
    instead. Handle both so the preflight never fails on a schema that is
    actually correct.
    """
    if isinstance(agg, str):
        return [col.strip() for col in agg.strip("{}").split(",") if col.strip()]
    return list(agg)


def _unique_constraint_columns(cur, table: str) -> list[list[str]]:
    """Return one sorted column-list per PRIMARY KEY / UNIQUE constraint on `table`."""
    cur.execute(
        """
        SELECT array_agg(kcu.column_name::text ORDER BY kcu.column_name) AS cols
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_name = %s
          AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        GROUP BY tc.constraint_name
        """,
        (table,),
    )
    return [_as_column_list(row[0]) for row in cur.fetchall()]


def preflight_scribe_schema(scribe_cur) -> None:
    """Verify the Scribe destination schema. Log + ``sys.exit(1)`` on failure."""
    problems: list[str] = []

    if not _table_exists(scribe_cur, "entities"):
        problems.append("table `entities` is missing")
    else:
        cols = _unique_constraint_columns(scribe_cur, "entities")
        if ["entity_id"] not in cols:
            problems.append("table `entities` has no UNIQUE/PRIMARY KEY on `entity_id`")

    if not _table_exists(scribe_cur, "states_raw"):
        problems.append("table `states_raw` is missing")
    else:
        cols = _unique_constraint_columns(scribe_cur, "states_raw")
        if ["metadata_id", "time"] not in cols:
            problems.append(
                "table `states_raw` has no PRIMARY KEY/UNIQUE on (metadata_id, time)"
            )

    if problems:
        logging.error("❌ Scribe schema preflight failed:")
        for p in problems:
            logging.error("   - %s", p)
        logging.error("")
        logging.error(_GUIDANCE)
        sys.exit(1)

    logging.info("✅ Scribe schema preflight OK.")
