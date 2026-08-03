"""Tests for migration module."""

import pytest
from unittest.mock import AsyncMock, patch
from custom_components.scribe import migration


@pytest.mark.asyncio
async def test_migrate_database_logic(hass, mock_pool, mock_db_connection):
    """Test that migrate_database calls sub-migrations."""
    # Mock states_raw table check returning True to skip 60s delay
    mock_db_connection.fetchval.return_value = True

    with (
        patch(
            "custom_components.scribe.migration._migrate_states_raw_constraints",
            new_callable=AsyncMock,
        ) as mock_constraints,
        patch(
            "custom_components.scribe.migration.migrate_states_data",
            new_callable=AsyncMock,
        ) as mock_data,
        patch(
            "custom_components.scribe.migration._convert_to_hypertable",
            new_callable=AsyncMock,
        ) as mock_hyper,
        patch(
            "custom_components.scribe.migration._migrate_events_pk",
            new_callable=AsyncMock,
        ) as mock_events,
        patch(
            "custom_components.scribe.migration._check_timescaledb",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):  # Skip sleep
        await migration.migrate_database(hass, mock_pool, True, True)

        mock_constraints.assert_called_once()
        mock_data.assert_called_once_with(mock_pool)
        mock_hyper.assert_called_once()
        mock_events.assert_called_once()


@pytest.mark.asyncio
async def test_migrate_states_raw_already_done(mock_pool, mock_db_connection):
    """Test that migration skips if PK already exists."""
    # Mock PK check returning a row (exists)
    mock_db_connection.fetchrow.return_value = {"exists": 1}

    await migration._migrate_states_raw_constraints(mock_pool, False)

    # verify check was made
    call_args = mock_db_connection.fetchrow.call_args[0][0]
    assert "conname = 'states_raw_pkey'" in call_args

    # verify no other calls (like SELECT COUNT(*) for dups)
    assert mock_db_connection.fetchval.call_count == 0


@pytest.mark.asyncio
async def test_migrate_states_raw_with_duplicates(mock_pool, mock_db_connection):
    """Test migration with duplicates to clean."""
    # 1. Check PK -> None (not done)
    # 2. Check duplicates -> 5 (found)
    # 3. Delete duplicates

    mock_db_connection.fetchrow.return_value = None
    mock_db_connection.fetchval.return_value = 5
    mock_db_connection.execute.return_value = "DELETE 5"

    await migration._migrate_states_raw_constraints(mock_pool, False)

    # Verify SQL calls
    assert mock_db_connection.fetchrow.called
    assert mock_db_connection.fetchval.called

    # Verify duplicates were deleted
    delete_call = [
        c
        for c in mock_db_connection.execute.mock_calls
        if "DELETE FROM states_raw" in str(c)
    ]
    assert len(delete_call) > 0

    # Verify constraints added
    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]
    assert any("ADD PRIMARY KEY" in s for s in execute_calls)
    assert any("ADD CONSTRAINT fk_states_raw_entity" in s for s in execute_calls)


@pytest.mark.asyncio
async def test_migrate_events_pk_already_done(mock_pool, mock_db_connection):
    """Test event migration skips if 'id' column exists."""
    # 1. Check table events exists -> True
    # 2. Check is hypertable -> False
    # 3. Check column id exists -> True
    mock_db_connection.fetchrow.side_effect = [{"exists": 1}, None, {"exists": 1}]

    await migration._migrate_events_pk(mock_pool, True)

    # verify no alter table
    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]
    assert not any("ADD COLUMN id" in s for s in execute_calls)


@pytest.mark.asyncio
async def test_migrate_events_pk_adds_column(mock_pool, mock_db_connection):
    """Test event migration adds column if missing."""
    # 1. Check table events exists -> True
    # 2. Check is hypertable -> False
    # 3. Check column id exists -> False
    mock_db_connection.fetchrow.side_effect = [{"exists": 1}, None, None]

    await migration._migrate_events_pk(mock_pool, True)

    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]
    assert any("ADD COLUMN id BIGSERIAL PRIMARY KEY" in s for s in execute_calls)


@pytest.mark.asyncio
async def test_migrate_states_raw_disables_and_reenables_compression(
    mock_pool, mock_db_connection
):
    """Test that compression is disabled before adding constraints and re-enabled after."""
    mock_db_connection.fetchrow.side_effect = [
        None,  # PK check
        {"compression_enabled": True},  # compression check: enabled
    ]
    mock_db_connection.fetchval.return_value = 0  # no duplicates
    mock_db_connection.execute.return_value = "OK"

    await migration._migrate_states_raw_constraints(mock_pool, True)

    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]

    # Verify compression was disabled
    assert any("timescaledb.compress = false" in s for s in execute_calls)
    assert any("remove_compression_policy" in s for s in execute_calls)

    # Verify constraints were added
    assert any("ADD PRIMARY KEY" in s for s in execute_calls)
    assert any("ADD CONSTRAINT fk_states_raw_entity" in s for s in execute_calls)

    # Verify compression was re-enabled
    assert any("timescaledb.compress," in s for s in execute_calls)
    assert any("add_compression_policy" in s for s in execute_calls)


@pytest.mark.asyncio
async def test_migrate_states_raw_enables_compression_even_if_not_previously_on(
    mock_pool, mock_db_connection
):
    """Test that compression is always enabled after constraints, even if it wasn't on before."""
    mock_db_connection.fetchrow.side_effect = [
        None,  # PK check
        {"compression_enabled": False},  # hypertable exists but compression was off
    ]
    mock_db_connection.fetchval.return_value = 0  # no duplicates
    mock_db_connection.execute.return_value = "OK"

    await migration._migrate_states_raw_constraints(mock_pool, True)

    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]

    # Verify compression was NOT disabled (it wasn't on)
    assert not any("timescaledb.compress = false" in s for s in execute_calls)

    # But compression should still be enabled at the end
    assert any("timescaledb.compress," in s for s in execute_calls)
    assert any("add_compression_policy" in s for s in execute_calls)


@pytest.mark.asyncio
async def test_migrate_states_data_drops_legacy_cascade(mock_pool, mock_db_connection):
    """Test that states_legacy is dropped with CASCADE."""
    # 1. Check legacy exists -> True
    # 2. MIN/MAX time query -> None (empty table)
    mock_db_connection.fetchval.return_value = True  # legacy exists
    mock_db_connection.fetchrow.return_value = (None, None)  # empty table

    await migration.migrate_states_data(mock_pool)

    execute_calls = [str(c) for c in mock_db_connection.execute.mock_calls]
    assert any("DROP TABLE states_legacy CASCADE" in s for s in execute_calls)
