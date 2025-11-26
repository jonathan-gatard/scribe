"""Test configuration for Scribe."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield

@pytest.fixture
def mock_setup_entry():
    """Mock setup entry."""
    with patch("custom_components.scribe.async_setup_entry", return_value=True) as mock_setup:
        yield mock_setup

@pytest.fixture
def mock_db_connection():
    """Mock database connection."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.begin = AsyncMock()
    # Mock context manager for begin()
    mock_conn.begin.return_value.__aenter__.return_value = mock_conn
    mock_conn.begin.return_value.__aexit__.return_value = None
    return mock_conn

@pytest.fixture
def mock_engine(mock_db_connection):
    """Mock database engine."""
    mock_engine = MagicMock()
    
    # connect() returns an async context manager
    mock_engine.connect.return_value.__aenter__.return_value = mock_db_connection
    mock_engine.connect.return_value.__aexit__.return_value = None
    
    # begin() returns an async context manager
    mock_engine.begin.return_value.__aenter__.return_value = mock_db_connection
    mock_engine.begin.return_value.__aexit__.return_value = None
    
    # dispose() is async
    mock_engine.dispose = AsyncMock()
    
    return mock_engine

@pytest.fixture(autouse=True)
def mock_create_async_engine(mock_engine):
    """Mock create_async_engine."""
    with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=mock_engine) as mock_create, \
         patch("custom_components.scribe.writer.create_async_engine", return_value=mock_engine):
        yield mock_create

@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.scribe.const import DOMAIN, CONF_DB_URL, CONF_RECORD_STATES, CONF_RECORD_EVENTS
    
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DB_URL: "postgresql://user:pass@host/db",
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True,
        },
        options={
            CONF_RECORD_STATES: True,
            CONF_RECORD_EVENTS: True,
        },
        entry_id="test_entry_id"
    )
    entry.setup_lock = None
    return entry
