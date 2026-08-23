import pytest
from custom_components.scribe.writer import ScribeWriter, WriterConfig

# Using mock_pool from conftest.py


@pytest.fixture
async def writer(hass, mock_pool):
    """Create a writer instance."""
    writer = ScribeWriter(
        hass,
        WriterConfig(
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
        ),
    )
    writer._pool = mock_pool
    writer._connected = True
    yield writer
    if writer._task:
        await writer.stop()


@pytest.mark.asyncio
async def test_query_select_valid(writer, mock_db_connection):
    """Test valid SELECT query."""
    mock_db_connection.fetch.return_value = [{"id": 1, "name": "test"}]

    result = await writer.query("SELECT * FROM states")

    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["name"] == "test"
    assert mock_db_connection.fetch.called


@pytest.mark.asyncio
async def test_ensure_read_only_transaction(writer, mock_db_connection):
    """Test that the transaction is set to read-only."""
    mock_db_connection.fetch.return_value = []

    await writer.query("SELECT * FROM states")

    # Verify that SET LOCAL TRANSACTION READ ONLY was called
    calls = mock_db_connection.execute.call_args_list

    has_read_only = False
    for call in calls:
        if "SET LOCAL TRANSACTION READ ONLY" in str(call):
            has_read_only = True
            break

    assert has_read_only, f"Read-only transaction was not enforced. Calls: {calls}"


@pytest.mark.asyncio
async def test_query_whitespace(writer, mock_db_connection):
    """Test query with leading whitespace."""
    mock_db_connection.fetch.return_value = []
    await writer.query("  SELECT * FROM states")
    assert mock_db_connection.fetch.called


@pytest.mark.asyncio
async def test_query_case_insensitive(writer, mock_db_connection):
    """Test query case insensitivity."""
    mock_db_connection.fetch.return_value = []
    await writer.query("select * from states")
    assert mock_db_connection.fetch.called


@pytest.mark.asyncio
async def test_query_no_connection(hass):
    """Test query without connection."""
    writer = ScribeWriter(
        hass,
        WriterConfig(
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
        ),
    )
    # Don't start writer, so no engine

    with pytest.raises(RuntimeError, match="Database not connected"):
        await writer.query("SELECT * FROM states")
