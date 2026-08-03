"""Test configuration for Scribe."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_setup_entry():
    """Mock setup entry."""
    with patch(
        "custom_components.scribe.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


@pytest.fixture
def mock_db_connection():
    """Mock asyncpg database connection."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.executemany = AsyncMock()
    mock_conn.copy_records_to_table = AsyncMock()
    mock_conn.fetchval = AsyncMock()
    mock_conn.fetchrow = AsyncMock()
    mock_conn.fetch = AsyncMock()

    # transaction() returns an async context manager
    tx_mock = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=tx_mock)
    tx_mock.__aenter__.return_value = tx_mock
    tx_mock.__aexit__.return_value = None

    return mock_conn


@pytest.fixture
def mock_pool(mock_db_connection):
    """Mock asyncpg connection pool."""
    mock_pool = MagicMock()

    # acquire() returns an async context manager
    acquire_ctx = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    acquire_ctx.__aenter__.return_value = mock_db_connection
    acquire_ctx.__aexit__.return_value = None

    # close() is async
    mock_pool.close = AsyncMock()

    return mock_pool


@pytest.fixture(autouse=True)
def mock_create_pool(mock_pool):
    """Mock create_pool."""
    with patch(
        "custom_components.scribe.writer.asyncpg.create_pool",
        new_callable=AsyncMock,
        return_value=mock_pool,
    ) as mock_create:
        yield mock_create


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.scribe.const import (
        DOMAIN,
        CONF_DB_URL,
        CONF_RECORD_STATES,
        CONF_RECORD_EVENTS,
    )

    mock_entry = MockConfigEntry(
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
        entry_id="test_entry_id",
    )
    mock_entry.setup_lock = MagicMock()
    mock_entry.setup_lock.locked.return_value = False
    # from homeassistant.config_entries import ConfigEntryState
    # mock_entry.state = ConfigEntryState.LOADED
    return mock_entry
