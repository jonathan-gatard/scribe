"""Test configuration for Scribe."""
import pytest
from unittest.mock import MagicMock

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield

@pytest.fixture
def mock_setup_entry():
    """Mock setup entry."""
    with pytest.mock.patch("custom_components.scribe.async_setup_entry", return_value=True) as mock_setup:
        yield mock_setup
