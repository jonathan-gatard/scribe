import pytest
from unittest.mock import MagicMock, AsyncMock
from custom_components.scribe.writer import ScribeWriter

@pytest.fixture
def mock_engine():
    """Mock the SQLAlchemy engine."""
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = AsyncMock()
    return engine

@pytest.fixture
async def writer(hass, mock_engine):
    """Create a writer instance."""
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=2,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert=None,
        ssl_cert_file=None,
        ssl_key_file=None,
        engine=mock_engine
    )
    yield writer
    if writer._task:
        await writer.stop()

@pytest.mark.asyncio
async def test_query_select_valid(writer, mock_engine):
    """Test valid SELECT query."""
    mock_conn = mock_engine.connect.return_value.__aenter__.return_value
    
    # Mock result
    mock_result = MagicMock()
    mock_row = MagicMock()
    mock_row._mapping = {"id": 1, "name": "test"}
    mock_result.__iter__.return_value = [mock_row]
    mock_conn.execute.return_value = mock_result

    result = await writer.query("SELECT * FROM states")
    
    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["name"] == "test"
    assert mock_conn.execute.called

@pytest.mark.asyncio
async def test_ensure_read_only_transaction(writer, mock_engine):
    """Test that the transaction is set to read-only."""
    mock_conn = mock_engine.connect.return_value.__aenter__.return_value
    mock_result = MagicMock()
    mock_result.__iter__.return_value = []
    mock_conn.execute.return_value = mock_result

    await writer.query("SELECT * FROM states")
    
    # Verify that SET LOCAL TRANSACTION READ ONLY was called
    calls = mock_conn.execute.call_args_list
    
    has_read_only = False
    for call in calls:
        if call.args:
            # call.args[0] is the sqlalchemy TextClause object
            # str(call.args[0]) returns the actual SQL string
            if "SET LOCAL TRANSACTION READ ONLY" in str(call.args[0]):
                has_read_only = True
                break
                
    assert has_read_only, f"Read-only transaction was not enforced. Calls: {calls}"

@pytest.mark.asyncio
async def test_query_whitespace(writer, mock_engine):
    """Test query with leading whitespace."""
    mock_conn = mock_engine.connect.return_value.__aenter__.return_value
    mock_result = MagicMock()
    mock_result.__iter__.return_value = []
    mock_conn.execute.return_value = mock_result

    await writer.query("  SELECT * FROM states")
    assert mock_conn.execute.called

@pytest.mark.asyncio
async def test_query_case_insensitive(writer, mock_engine):
    """Test query case insensitivity."""
    mock_conn = mock_engine.connect.return_value.__aenter__.return_value
    mock_result = MagicMock()
    mock_result.__iter__.return_value = []
    mock_conn.execute.return_value = mock_result

    await writer.query("select * from states")
    assert mock_conn.execute.called

@pytest.mark.asyncio
async def test_query_no_connection(hass):
    """Test query without connection."""
    writer = ScribeWriter(
        hass=hass,
        db_url="postgresql://user:pass@host/db",
        chunk_interval="7 days",
        compress_after="60 days",
        record_states=True,
        record_events=True,
        batch_size=2,
        flush_interval=5,
        max_queue_size=10000,
        buffer_on_failure=True,
        table_name_states="states",
        table_name_events="events",
        ssl_root_cert=None,
        ssl_cert_file=None,
        ssl_key_file=None
    )
    # Don't start writer, so no engine
    
    with pytest.raises(RuntimeError, match="Database not connected"):
        await writer.query("SELECT * FROM states")
