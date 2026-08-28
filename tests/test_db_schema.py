"""Test the configurable database schema (issue #53)."""

import pytest
from unittest.mock import AsyncMock, patch

from custom_components.scribe.writer import (
    ScribeWriter,
    WriterConfig,
    _validate_schema_name,
)


def _writer(hass, mock_pool, **kwargs):
    writer = ScribeWriter(
        hass,
        WriterConfig(
            db_url="postgresql://user:pass@host/db",
            record_states=True,
            record_events=True,
            **kwargs,
        ),
    )
    writer._pool = mock_pool
    return writer


def _executed(mock_db_connection):
    return [c.args[0] for c in mock_db_connection.execute.mock_calls if c.args]


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["scribe", "_private", "ha_history_2", "MixedCase"])
def test_validate_schema_name_accepts_plain_identifiers(value):
    assert _validate_schema_name(value) == value


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_schema_means_leave_the_connection_alone(value):
    assert _validate_schema_name(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "2fast",
        "my schema",
        "my-schema",
        'public"; DROP SCHEMA public CASCADE; --',
        "scribe, public",
    ],
)
def test_validate_schema_name_refuses_everything_else(value):
    with pytest.raises(ValueError):
        _validate_schema_name(value)


def test_an_invalid_schema_is_refused_before_the_writer_exists(hass):
    """The name reaches DDL, so it must never survive construction."""
    with pytest.raises(ValueError):
        ScribeWriter(
            hass,
            WriterConfig(db_url="postgresql://u:p@h/d", db_schema="bad name"),
        )


# ---------------------------------------------------------------------------
# search_path
# ---------------------------------------------------------------------------


async def _pool_kwargs(writer):
    """Run `_connect` far enough to capture what the pool is created with."""
    captured = {}

    async def fake_create_pool(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here: only the pool arguments are under test")

    with patch(
        "custom_components.scribe.writer.asyncpg.create_pool",
        side_effect=fake_create_pool,
    ):
        await writer._connect()

    return captured


@pytest.mark.asyncio
async def test_a_configured_schema_goes_first_on_the_search_path(hass, mock_pool):
    """Every relation is named unqualified, so search_path is what moves them."""
    writer = _writer(hass, mock_pool, db_schema="scribe")

    settings = (await _pool_kwargs(writer))["server_settings"]

    assert settings["search_path"].startswith('"scribe"')


@pytest.mark.asyncio
async def test_public_stays_reachable_for_the_timescaledb_functions(hass, mock_pool):
    """create_hypertable and friends live in the extension's schema."""
    writer = _writer(hass, mock_pool, db_schema="scribe")

    settings = (await _pool_kwargs(writer))["server_settings"]

    assert settings["search_path"].endswith(", public")


@pytest.mark.asyncio
async def test_the_search_path_survives_the_pools_connection_reset(hass, mock_pool):
    """asyncpg runs RESET ALL on release, which undoes a `SET` from `init`.

    A startup parameter is what RESET ALL resets *to*, so it has to be one:
    from `init` the first acquire would use the schema and every later one
    would silently write to `public`.
    """
    writer = _writer(hass, mock_pool, db_schema="scribe")

    kwargs = await _pool_kwargs(writer)
    conn = AsyncMock()
    await kwargs["init"](conn)

    assert "search_path" in kwargs.get("server_settings", {})
    assert not [
        c.args[0]
        for c in conn.execute.mock_calls
        if c.args and "search_path" in c.args[0]
    ]


@pytest.mark.asyncio
async def test_no_schema_configured_never_touches_the_search_path(hass, mock_pool):
    """A DSN or role that sets its own search_path must be left as it is."""
    writer = _writer(hass, mock_pool)

    assert (await _pool_kwargs(writer))["server_settings"] is None


# ---------------------------------------------------------------------------
# _ensure_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_schema_is_created_before_anything_is_written(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    mock_db_connection.fetchval.return_value = "scribe"

    assert await writer._ensure_schema(mock_db_connection) is True
    assert 'CREATE SCHEMA IF NOT EXISTS "scribe"' in _executed(mock_db_connection)
    assert writer.active_schema == "scribe"


@pytest.mark.asyncio
async def test_a_schema_that_only_needs_usage_is_accepted(
    hass, mock_pool, mock_db_connection
):
    """CREATE may fail on a schema someone else already made and granted."""
    writer = _writer(hass, mock_pool, db_schema="scribe")
    mock_db_connection.execute.side_effect = Exception("permission denied")
    mock_db_connection.fetchval.return_value = "scribe"

    assert await writer._ensure_schema(mock_db_connection) is True
    assert writer.active_schema == "scribe"
    assert writer._schema_blocked is False


@pytest.mark.asyncio
async def test_an_unreachable_schema_stops_recording_instead_of_using_public(
    hass, mock_pool, mock_db_connection
):
    """PostgreSQL skips a missing search_path entry — it does not raise.

    Carrying on would write the whole history into `public` under a UI still
    showing the schema the user asked for.
    """
    writer = _writer(hass, mock_pool, db_schema="scribe")
    mock_db_connection.fetchval.return_value = "public"

    with patch.object(writer, "_report_issue") as report:
        assert await writer._ensure_schema(mock_db_connection) is False

    assert writer._schema_blocked is True
    assert writer._connected is False
    assert report.call_args.args[0] == "schema_unavailable"
    assert report.call_args.args[1] == "schema_unavailable"


@pytest.mark.asyncio
async def test_the_unreachable_schema_issue_names_both_schemas(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    mock_db_connection.fetchval.return_value = "public"

    with patch.object(writer, "_report_issue") as report:
        await writer._ensure_schema(mock_db_connection)

    placeholders = report.call_args.args[2]
    assert placeholders["schema"] == "scribe"
    assert placeholders["fallback"] == "public"


@pytest.mark.asyncio
async def test_nothing_is_queued_while_the_schema_is_unusable(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer._running = True
    mock_db_connection.fetchval.return_value = "public"

    with patch.object(writer, "_report_issue"):
        await writer._ensure_schema(mock_db_connection)
    writer.enqueue({"type": "state", "entity_id": "sensor.x"})

    assert len(writer._queue) == 0


@pytest.mark.asyncio
async def test_no_tables_are_created_in_the_wrong_schema(
    hass, mock_pool, mock_db_connection
):
    """init_db must stop at the schema step, before any DDL."""
    writer = _writer(hass, mock_pool, db_schema="scribe")
    mock_db_connection.fetchval.return_value = "public"

    with patch.object(writer, "_report_issue"):
        await writer.init_db()

    assert not [s for s in _executed(mock_db_connection) if "CREATE TABLE" in s]
    assert writer._connected is False


@pytest.mark.asyncio
async def test_the_issue_clears_once_the_schema_is_reachable(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer._schema_blocked = True
    mock_db_connection.fetchval.return_value = "scribe"

    with patch.object(writer, "_clear_issue") as clear:
        await writer._ensure_schema(mock_db_connection)

    assert "schema_unavailable" in [c.args[0] for c in clear.mock_calls]
    assert writer._schema_blocked is False


@pytest.mark.asyncio
async def test_an_unconfigured_writer_follows_the_connection(
    hass, mock_pool, mock_db_connection
):
    """A DSN carrying `options=-csearch_path=...` must still be filtered on."""
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = "somewhere_else"

    assert await writer._ensure_schema(mock_db_connection) is True
    assert writer.active_schema == "somewhere_else"
    assert not [s for s in _executed(mock_db_connection) if "CREATE SCHEMA" in s]


# ---------------------------------------------------------------------------
# Catalog lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_detection_only_looks_in_the_active_schema(
    hass, mock_pool, mock_db_connection
):
    """Another installation's `public.states` must not block this one."""
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer.active_schema = "scribe"

    await writer._detect_legacy_schema(mock_db_connection)

    for call in mock_db_connection.fetchval.mock_calls:
        assert "table_schema = $1" in call.args[0]
        assert call.args[1] == "scribe"


@pytest.mark.asyncio
async def test_storage_checks_only_look_at_this_schemas_hypertables(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer.active_schema = "scribe"
    mock_db_connection.fetchval.return_value = True

    await writer._verify_storage_features("states_raw")

    for call in mock_db_connection.fetchval.mock_calls:
        assert "hypertable_schema = $2" in call.args[0]
        assert call.args[2] == "scribe"


@pytest.mark.asyncio
async def test_the_retention_policy_is_read_from_this_schema_only(
    hass, mock_pool, mock_db_connection
):
    """Two Scribe schemas in one database each own their own policy."""
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer.active_schema = "scribe"
    mock_db_connection.fetchval.return_value = None

    await writer._apply_retention_policy("states_raw", "30 days")

    lookup = mock_db_connection.fetchval.mock_calls[0]
    assert "hypertable_schema = $2" in lookup.args[0]
    assert lookup.args[2] == "scribe"


@pytest.mark.asyncio
async def test_chunk_statistics_do_not_count_another_schemas_chunks(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool, db_schema="scribe")
    writer.active_schema = "scribe"

    await writer._get_states_chunk_stats()

    call = mock_db_connection.fetchrow.mock_calls[0]
    assert "hypertable_schema = $1" in call.args[0]
    assert call.args[1] == "scribe"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def _advanced_step(hass, entry):
    """Walk the options flow to its last step."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for _ in range(4):  # init → performance → stats → metadata → advanced
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    assert result["step_id"] == "advanced"
    return result


@pytest.mark.asyncio
async def test_options_flow_rejects_a_name_postgresql_would_not_accept(
    hass, mock_config_entry
):
    """The user finds out at the form, not as a writer that records nothing."""
    from homeassistant.data_entry_flow import FlowResultType

    result = await _advanced_step(hass, mock_config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"db_schema": "my schema"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"db_schema": "invalid_schema"}


@pytest.mark.asyncio
async def test_options_flow_stores_a_valid_schema(hass, mock_config_entry):
    from homeassistant.data_entry_flow import FlowResultType

    result = await _advanced_step(hass, mock_config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"db_schema": "scribe"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["db_schema"] == "scribe"


@pytest.mark.asyncio
async def test_the_schema_field_can_be_left_empty(hass, mock_config_entry):
    """Empty is the default and must stay a valid answer."""
    from homeassistant.data_entry_flow import FlowResultType

    result = await _advanced_step(hass, mock_config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"db_schema": ""}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["db_schema"] == ""
