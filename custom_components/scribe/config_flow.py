"""Config flow for Scribe integration.

This module handles the UI configuration for Scribe.
It now only supports import from configuration.yaml.
Manual configuration via UI is disabled to enforce YAML config stability.
"""
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class ScribeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scribe."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        return self.async_abort(reason="ui_configuration_disabled")

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from YAML."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates=user_input)
        return self.async_create_entry(title="Scribe", data=user_input)

