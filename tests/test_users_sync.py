"""Test users sync logic."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.config_entries import ConfigEntry
from custom_components.scribe.const import DOMAIN, CONF_DB_URL


@pytest.fixture
def mock_config_entry():
    """Mock ConfigEntry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.domain = DOMAIN
    entry.data = {CONF_DB_URL: "postgresql://user:pass@host/db"}
    entry.options = {}
    entry.entry_id = "test_entry"
    entry.title = "Scribe"
    entry.setup_lock = MagicMock()
    entry.setup_lock.locked.return_value = False
    from homeassistant.config_entries import ConfigEntryState

    entry.state = ConfigEntryState.LOADED
    return entry


@pytest.mark.asyncio
async def test_users_sync(hass, mock_config_entry):
    """Test that users are synced on setup."""
    from custom_components.scribe import async_setup_entry

    # Mock HA Users
    mock_user = MagicMock()
    mock_user.id = "user_123"
    mock_user.name = "Test User"
    mock_user.is_owner = True
    mock_user.is_active = True
    mock_user.system_generated = False
    mock_user.groups = []

    hass.auth.users = [mock_user]

    with (
        patch("custom_components.scribe.ScribeWriter") as mock_writer_cls,
        patch(
            "homeassistant.auth.AuthManager.async_get_users", new_callable=AsyncMock
        ) as mock_get_users,
    ):
        mock_writer = mock_writer_cls.return_value
        mock_writer.start = AsyncMock()
        mock_writer.stop = AsyncMock()
        mock_writer.write_users = AsyncMock()

        # Configure mock to return our list
        mock_get_users.return_value = [mock_user]

        await async_setup_entry(hass, mock_config_entry)

        mock_writer.write_users.assert_called_once()
        users_arg = mock_writer.write_users.call_args[0][0]
        assert len(users_arg) == 1
        assert users_arg[0]["user_id"] == "user_123"
        assert users_arg[0]["name"] == "Test User"
