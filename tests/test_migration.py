"""Tests for migration module."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from sqlalchemy import text
from custom_components.scribe import migration

class AsyncContextManagerMock:
    """Mock for async context manager."""
    def __init__(self, return_value):
        self.return_value = return_value

    async def __aenter__(self):
        return self.return_value

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

@pytest.fixture
def mock_engine():
    """Mock the SQLAlchemy engine."""
    engine = AsyncMock()
    connection = AsyncMock()
    result = MagicMock()
    
    # Configure result
    connection.execute.return_value = result
    
    # Configure context managers properly
    # engine.begin() and connect() are NOT coroutines, they return async context managers
    # So we use MagicMock, not AsyncMock for the method itself
    engine.begin = MagicMock(return_value=AsyncContextManagerMock(connection))
    engine.connect = MagicMock(return_value=AsyncContextManagerMock(connection))
    
    return engine, connection, result

@pytest.mark.asyncio
async def test_migrate_database_calls_sub_migrations(hass, mock_engine):
    """Test that migrate_database calls sub-migrations."""
    engine, _, _ = mock_engine
    
    with patch("custom_components.scribe.migration._migrate_states_raw_constraints", new_callable=AsyncMock) as mock_states, \
         patch("custom_components.scribe.migration._migrate_events_pk", new_callable=AsyncMock) as mock_events, \
         patch("asyncio.sleep", new_callable=AsyncMock): # Skip sleep
        
        await migration.migrate_database(hass, engine, True, True)
        
        mock_states.assert_called_once_with(engine)
        mock_events.assert_called_once_with(engine)

@pytest.mark.asyncio
async def test_migrate_states_raw_already_done(mock_engine):
    """Test that migration skips if PK already exists."""
    engine, connection, result = mock_engine
    
    # Mock PK check returning true
    result.fetchone.return_value = [1]
    
    await migration._migrate_states_raw_constraints(engine)
    
    # verify check was made
    call_args = connection.execute.call_args[0][0].text
    assert "conname = 'states_raw_pkey'" in call_args
    
    # verify no other calls (like ADD PRIMARY KEY)
    assert connection.execute.call_count == 1

@pytest.mark.asyncio
async def test_migrate_states_raw_with_duplicates(mock_engine):
    """Test migration with duplicates to clean."""
    engine, connection, result = mock_engine
    
    # 1. Check PK -> None (not done)
    # 2. Check duplicates -> 5 (found)
    # 3. Delete duplicates
    # 4. Add PK
    # 5. Add FK
    
    # Configure return values for sequential calls
    # fetchone is called for PK check
    # scalar is called for dup count
    result.fetchone.side_effect = [None] 
    result.scalar.return_value = 5
    result.rowcount = 5 # for delete result
    
    await migration._migrate_states_raw_constraints(engine)
    
    # Verify SQL calls
    sql_calls = [c[0][0].text for c in connection.execute.call_args_list]
    
    assert any("SELECT 1 FROM pg_constraint" in s for s in sql_calls)
    assert any("SELECT COUNT(*)" in s for s in sql_calls)
    assert any("DELETE FROM states_raw" in s for s in sql_calls)
    assert any("ADD PRIMARY KEY" in s for s in sql_calls)
    assert any("ADD CONSTRAINT fk_states_raw_entity" in s for s in sql_calls)

@pytest.mark.asyncio
async def test_migrate_events_pk_already_done(mock_engine):
    """Test event migration skips if 'id' column exists."""
    engine, connection, result = mock_engine
    
    # 1. Check table events exists -> True
    # 2. Check column id exists -> True
    result.fetchone.side_effect = [[1], [1]]
    
    await migration._migrate_events_pk(engine)
    
    # verify check was made
    sql_calls = [c[0][0].text for c in connection.execute.call_args_list]
    assert any("column_name = 'id'" in s for s in sql_calls)
    
    # verify no alter table
    assert not any("ADD COLUMN id" in s for s in sql_calls)

@pytest.mark.asyncio
async def test_migrate_events_pk_adds_column(mock_engine):
    """Test event migration adds column if missing."""
    engine, connection, result = mock_engine
    
    # 1. Check table events exists -> True
    # 2. Check column id exists -> None
    result.fetchone.side_effect = [[1], None]
    
    await migration._migrate_events_pk(engine)
    
    sql_calls = [c[0][0].text for c in connection.execute.call_args_list]
    assert any("ADD COLUMN id BIGSERIAL PRIMARY KEY" in s for s in sql_calls)
