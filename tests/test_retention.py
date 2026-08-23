"""Test the configurable retention policy (issue #53)."""

import pytest
from unittest.mock import patch

from custom_components.scribe.writer import (
    ScribeWriter,
    _validate_interval,
    WriterConfig,
)


def _writer(hass, mock_pool, **kwargs):
    writer = ScribeWriter(
        hass,
        WriterConfig(
            db_url="postgresql://user:pass@host/db",
            chunk_interval="7 days",
            compress_after="7 days",
            record_states=True,
            record_events=True,
            batch_size=2,
            flush_interval=5,
            max_queue_size=10,
            buffer_on_failure=True,
            table_name_states="states",
            table_name_events="events",
            **kwargs,
        ),
    )
    writer._pool = mock_pool
    return writer


def _executed(mock_db_connection):
    return [c.args[0] for c in mock_db_connection.execute.mock_calls if c.args]


@pytest.mark.parametrize(
    "value", ["30 days", "1 year", "6 months", " 12 hours ", "1 year 6 months"]
)
def test_validate_interval_accepts_plain_intervals(value):
    assert _validate_interval(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "forever",
        "30",
        "days",
        "30 days'); DROP TABLE states_raw; --",
        "1 day; SELECT 1",
    ],
)
def test_validate_interval_refuses_everything_else(value):
    with pytest.raises(ValueError):
        _validate_interval(value)


@pytest.mark.asyncio
async def test_no_retention_configured_leaves_no_policy(
    hass, mock_pool, mock_db_connection
):
    """Empty setting on a table with no policy: nothing to do, nothing said."""
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = None

    await writer._apply_retention_policy("states_raw", "")

    assert not [s for s in _executed(mock_db_connection) if "retention" in s]


@pytest.mark.asyncio
async def test_clearing_the_setting_removes_the_policy(
    hass, mock_pool, mock_db_connection
):
    """Emptying the field must actually stop chunks from being dropped."""
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = "30 days"

    await writer._apply_retention_policy("states_raw", "")

    assert any(
        "remove_retention_policy('states_raw'" in s
        for s in _executed(mock_db_connection)
    )
    assert not any("add_retention_policy" in s for s in _executed(mock_db_connection))


@pytest.mark.asyncio
async def test_retention_added_when_none_exists(hass, mock_pool, mock_db_connection):
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = None

    await writer._apply_retention_policy("states_raw", "365 days")

    assert any(
        "add_retention_policy('states_raw', INTERVAL '365 days')" in s
        for s in _executed(mock_db_connection)
    )


@pytest.mark.asyncio
async def test_unchanged_retention_is_not_recreated(
    hass, mock_pool, mock_db_connection
):
    """Re-creating the job on every restart would keep postponing its next run."""
    writer = _writer(hass, mock_pool)

    async def fetchval(sql, *args):
        if "timescaledb_information.jobs" in sql:
            return "1 mon"
        if "::interval" in sql:  # the equality check TimescaleDB does for us
            return True
        return None

    mock_db_connection.fetchval.side_effect = fetchval

    await writer._apply_retention_policy("states_raw", "1 month")

    assert not [s for s in _executed(mock_db_connection) if "retention" in s]


@pytest.mark.asyncio
async def test_changed_retention_replaces_the_policy(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)

    async def fetchval(sql, *args):
        if "timescaledb_information.jobs" in sql:
            return "30 days"
        if "::interval" in sql:
            return False
        return None

    mock_db_connection.fetchval.side_effect = fetchval

    await writer._apply_retention_policy("states_raw", "90 days")

    executed = _executed(mock_db_connection)
    assert any("remove_retention_policy('states_raw'" in s for s in executed)
    assert any(
        "add_retention_policy('states_raw', INTERVAL '90 days')" in s for s in executed
    )


@pytest.mark.asyncio
async def test_invalid_interval_never_reaches_sql(hass, mock_pool, mock_db_connection):
    """A bad value must be refused, not interpolated into the policy statement."""
    writer = _writer(hass, mock_pool)

    with patch.object(writer, "_report_issue") as report:
        await writer._apply_retention_policy(
            "states_raw", "30 days'); DROP TABLE x; --"
        )

    assert not mock_db_connection.execute.mock_calls
    assert report.call_args.args[0] == "retention_failed_states_raw"


@pytest.mark.asyncio
async def test_failure_is_surfaced_in_repairs(hass, mock_pool, mock_db_connection):
    """Retention that silently didn't apply is a database growing unbounded."""
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.side_effect = Exception("function does not exist")

    with patch.object(writer, "_report_issue") as report:
        await writer._apply_retention_policy("events", "30 days")

    assert report.call_args.args[0] == "retention_failed_events"
    assert report.call_args.args[1] == "retention_failed"


@pytest.mark.asyncio
async def test_failed_retention_issue_clears_once_applied(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = None

    with patch.object(writer, "_clear_issue") as clear:
        await writer._apply_retention_policy("events", "30 days")

    clear.assert_called_once_with("retention_failed_events")


@pytest.mark.asyncio
async def test_each_table_gets_its_own_setting(hass, mock_pool, mock_db_connection):
    """States and events are configured independently."""
    writer = _writer(
        hass, mock_pool, retention_states="365 days", retention_events="30 days"
    )
    mock_db_connection.fetchval.return_value = None

    with patch.object(writer, "_apply_retention_policy") as apply:
        await writer._init_hypertable(
            "states_raw", "metadata_id", writer.retention_states
        )
        await writer._init_hypertable("events", "event_type", writer.retention_events)

    assert [c.args for c in apply.mock_calls] == [
        ("states_raw", "365 days"),
        ("events", "30 days"),
    ]


def test_retention_defaults_to_off(hass, mock_pool):
    writer = _writer(hass, mock_pool)
    assert writer.retention_states == ""
    assert writer.retention_events == ""


@pytest.mark.asyncio
async def test_options_flow_rejects_an_invalid_interval(hass, mock_config_entry):
    """The user finds out at the form, not hours later in the logs."""
    from homeassistant.data_entry_flow import FlowResultType

    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    for _ in range(4):  # init → performance → stats → metadata → advanced
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    assert result["step_id"] == "advanced"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"retention_states": "forever"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"retention_states": "invalid_interval"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"retention_states": "365 days"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["retention_states"] == "365 days"


@pytest.mark.asyncio
async def test_plain_postgres_without_retention_stays_quiet(
    hass, mock_pool, mock_db_connection, caplog
):
    """The default config on plain PostgreSQL must not log an error each start.

    `timescaledb_information` does not exist there, and with no retention
    configured there is no policy to remove either — so there is nothing to
    report.
    """
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.side_effect = Exception(
        'relation "timescaledb_information.jobs" does not exist'
    )

    with patch.object(writer, "_report_issue") as report:
        await writer._apply_retention_policy("states_raw", "")

    assert not report.mock_calls
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]


async def _writer_config(hass, entry_data, entry_options, yaml_config=None):
    """Run async_setup_entry and return the WriterConfig it built."""
    from unittest.mock import MagicMock, AsyncMock
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState

    from custom_components.scribe import DOMAIN, async_setup_entry

    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = entry_data
    entry.options = entry_options
    entry.entry_id = "test_entry"
    entry.title = "Scribe"
    entry.state = ConfigEntryState.LOADED
    entry.setup_lock = MagicMock()
    entry.setup_lock.locked.return_value = False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["yaml_config"] = yaml_config or {}

    with (
        patch("custom_components.scribe.ScribeWriter") as writer_cls,
        patch(
            "homeassistant.auth.AuthManager.async_get_users",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        w = writer_cls.return_value
        for name in (
            "start",
            "stop",
            "write_users",
            "write_entities",
            "write_areas",
            "write_devices",
            "write_integrations",
        ):
            setattr(w, name, AsyncMock())
        await async_setup_entry(hass, entry)
        return writer_cls.call_args.args[1]


@pytest.mark.asyncio
async def test_retention_from_yaml_is_applied(hass):
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@h/d"},
        entry_options={},
        yaml_config={"retention_states": "365 days"},
    )
    assert cfg.retention_states == "365 days"


@pytest.mark.asyncio
async def test_removing_the_yaml_line_restores_forever(hass):
    """A YAML import copies its keys into entry.data; deleting the line must win.

    Otherwise the stale value goes on dropping chunks the user stopped asking
    to drop — the one setting where "leftover config" means lost history.
    """
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@h/d", "retention_states": "365 days"},
        entry_options={},
        yaml_config={},  # the line was removed from configuration.yaml
    )
    assert cfg.retention_states == ""


@pytest.mark.asyncio
async def test_ui_retention_still_applies(hass):
    """The options flow is the UI's storage, and it must keep working."""
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@h/d", "retention_states": "365 days"},
        entry_options={"retention_states": "30 days"},
        yaml_config={},
    )
    assert cfg.retention_states == "30 days"


@pytest.mark.asyncio
async def test_clearing_it_in_the_ui_wins_over_a_stale_import(hass):
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@h/d", "retention_states": "365 days"},
        entry_options={"retention_states": ""},
        yaml_config={},
    )
    assert cfg.retention_states == ""


# ---------------------------------------------------------------------------
# chunk_time_interval / compress_after — same "keep it in sync" contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_interval_is_left_alone_when_unchanged(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = True  # dimension already matches

    await writer._apply_chunk_interval("states_raw")

    assert not [s for s in _executed(mock_db_connection) if "set_chunk" in s]


@pytest.mark.asyncio
async def test_chunk_interval_is_resized_when_changed(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = False

    await writer._apply_chunk_interval("states_raw")

    assert any("set_chunk_time_interval" in s for s in _executed(mock_db_connection))


@pytest.mark.asyncio
async def test_chunk_interval_skipped_on_a_plain_table(
    hass, mock_pool, mock_db_connection
):
    """No row in `dimensions` means it is not a hypertable: nothing to sync."""
    writer = _writer(hass, mock_pool)
    mock_db_connection.fetchval.return_value = None

    await writer._apply_chunk_interval("states_raw")

    assert not mock_db_connection.execute.mock_calls


@pytest.mark.asyncio
async def test_compression_policy_replaced_when_changed(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)

    async def fetchval(sql, *args):
        if "policy_compression" in sql:
            return "60 days"
        if "::interval" in sql:
            return False
        return None

    mock_db_connection.fetchval.side_effect = fetchval

    await writer._apply_compression_policy("states_raw")

    executed = _executed(mock_db_connection)
    assert any("remove_compression_policy" in s for s in executed)
    assert any("add_compression_policy" in s for s in executed)


@pytest.mark.asyncio
async def test_compression_policy_untouched_when_unchanged(
    hass, mock_pool, mock_db_connection
):
    writer = _writer(hass, mock_pool)

    async def fetchval(sql, *args):
        if "policy_compression" in sql:
            return "7 days"
        if "::interval" in sql:
            return True
        return None

    mock_db_connection.fetchval.side_effect = fetchval

    await writer._apply_compression_policy("states_raw")

    assert not [s for s in _executed(mock_db_connection) if "compression_policy" in s]


@pytest.mark.asyncio
async def test_yaml_can_move_scribe_to_another_database(hass):
    """`db_url` follows the same precedence as everything else.

    The config entry keeps the URL it was created with, so when it was consulted
    first, editing the line in `configuration.yaml` did nothing at all — the one
    key a YAML edit could not change.
    """
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@old-host/scribe"},
        entry_options={},
        yaml_config={"db_url": "postgresql://u:p@new-host/scribe"},
    )
    assert cfg.db_url == "postgresql://u:p@new-host/scribe"


@pytest.mark.asyncio
async def test_entry_url_is_used_when_yaml_does_not_name_one(hass):
    """A UI-only install has no YAML block, and must keep working."""
    cfg = await _writer_config(
        hass,
        entry_data={"db_url": "postgresql://u:p@from-ui/scribe"},
        entry_options={},
        yaml_config={},
    )
    assert cfg.db_url == "postgresql://u:p@from-ui/scribe"
