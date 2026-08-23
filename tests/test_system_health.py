"""Test Scribe system health.

The panel is read when something looks wrong, so it has to describe the
database rather than the integration: "connected" used to be true whenever
Scribe was loaded at all, which said everything was fine while the database
was unreachable and nothing was being recorded.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.scribe.const import DOMAIN
from custom_components.scribe.system_health import async_register, system_health_info
from custom_components.scribe.writer import ScribeWriter, WriterConfig


@pytest.fixture
def writer(hass):
    return ScribeWriter(
        hass,
        WriterConfig(db_url="postgresql://scribe:hunter2@db.local:5432/scribe"),
    )


@pytest.mark.asyncio
async def test_system_health_register(hass):
    """Test system health registration."""
    register = MagicMock()
    async_register(hass, register)
    register.async_register_info.assert_called_with(system_health_info)


@pytest.mark.asyncio
async def test_the_version_is_the_one_that_is_installed(hass, writer):
    """It came from a key nothing ever set, so it always read "Unknown"."""
    hass.data[DOMAIN] = {"entry": {"writer": writer}}

    info = await system_health_info(hass)

    assert info["version"] not in ("Unknown", "")
    assert info["version"][0].isdigit()


@pytest.mark.asyncio
async def test_it_reports_the_database_not_the_integration(hass, writer):
    """A loaded integration whose database is down must not read as healthy."""
    hass.data[DOMAIN] = {"entry": {"writer": writer}}
    writer._connected = False
    writer._queue.append({"type": "state"})

    info = await system_health_info(hass)

    assert info["connected"] is False
    assert info["buffered_items"] == 1


@pytest.mark.asyncio
async def test_a_healthy_writer_reads_as_healthy(hass, writer):
    hass.data[DOMAIN] = {"entry": {"writer": writer}}
    writer._connected = True
    writer._has_timescaledb = True

    info = await system_health_info(hass)

    assert info["connected"] is True
    assert info["timescaledb"] is True


@pytest.mark.asyncio
async def test_the_credentials_never_reach_the_panel(hass, writer):
    """It shows which database, not how to log into it."""
    hass.data[DOMAIN] = {"entry": {"writer": writer}}

    info = await system_health_info(hass)

    assert info["database"] == "db.local:5432/scribe"
    assert "hunter2" not in str(info)


@pytest.mark.asyncio
async def test_a_setup_that_never_finished_still_reports(hass):
    hass.data[DOMAIN] = {"yaml_config": {}}

    info = await system_health_info(hass)

    assert info["database"] == "not set up"
    assert "connected" not in info
