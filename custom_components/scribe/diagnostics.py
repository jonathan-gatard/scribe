"""Diagnostics support for Scribe."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .binary_sensor import _redact_dsn
from .const import CONF_DB_URL, CONF_DB_PASSWORD, CONF_DB_USER, DOMAIN

TO_REDACT = {CONF_DB_URL, CONF_DB_PASSWORD, CONF_DB_USER}


def _writer_state(writer) -> dict[str, Any]:
    """What the writer is actually doing, for a bug report to stand on.

    Almost every question about Scribe is "why is nothing being recorded", and
    the answer is usually one of these lines: not connected, blocked on a
    pre-3.0 schema, buffering behind a failing write, or filtering everything
    out. Nothing here is sensitive — the database URL never appears, and the
    last driver error goes through the same redaction as the connectivity
    entity, since it is the one field that can quote a connection string.
    """
    return {
        "running": writer._running,
        "connected": writer._connected,
        "legacy_schema_blocked": writer._legacy_blocked,
        "schema_blocked": writer._schema_blocked,
        # Configured vs. in effect: they differ only on an install that never
        # set one, where the second is what the connection resolved to.
        "schema_configured": writer.db_schema or "(connection default)",
        "schema_in_use": writer.active_schema,
        "has_timescaledb": writer._has_timescaledb,
        "pool": "open" if writer._pool is not None else "none",
        "reconnect_delay_seconds": writer._connect_delay,
        "last_error": _redact_dsn(writer._last_error),
        "queue": {
            "size": len(writer._queue),
            "max_size": writer.max_queue_size,
            "buffer_on_failure": writer.buffer_on_failure,
            "dropped_since_start": writer._dropped_events,
            "consecutive_flush_failures": writer._consecutive_flush_failures,
        },
        "written": {
            "states": writer._states_written,
            "events": writer._events_written,
            "last_write_duration_seconds": writer._last_write_duration,
        },
        "storage": {
            "chunk_time_interval": writer.chunk_interval,
            "compress_after": writer.compress_after,
            "retention_states": writer.retention_states or "keep forever",
            "retention_events": writer.retention_events or "keep forever",
        },
        "recording": {
            "states": writer.record_states,
            "events": writer.record_events,
            "batch_size": writer.batch_size,
            "flush_interval": writer.flush_interval,
        },
        "entities_cached": len(writer._entity_id_map),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
    }

    # Absent when setup failed before the writer was built, which is itself
    # worth seeing in the report.
    writer = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("writer")
    diagnostics["writer"] = _writer_state(writer) if writer else "not started"

    return diagnostics
