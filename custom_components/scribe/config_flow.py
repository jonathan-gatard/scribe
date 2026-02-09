"""Config flow for Scribe integration.

This module handles the UI configuration for Scribe.
It now only supports import from configuration.yaml.
Manual configuration via UI is disabled to enforce YAML config stability.
"""
import logging

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

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ScribeOptionsFlowHandler(config_entry)


class ScribeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Scribe options."""

    def __init__(self, config_entry):
        """Initialize stats options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Manage the options."""
        from .const import (
            CONF_RECORD_STATES,
            CONF_RECORD_EVENTS,
            DEFAULT_RECORD_STATES,
            DEFAULT_RECORD_EVENTS,
            CONF_BATCH_SIZE,
            DEFAULT_BATCH_SIZE,
        )
        import voluptuous as vol

        if user_input is not None:
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(CONF_RECORD_EVENTS):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self.add_suggested_values_to_schema(
                         vol.Schema(
                            {
                                vol.Optional(CONF_BATCH_SIZE): int,
                                vol.Optional(CONF_RECORD_STATES): bool,
                                vol.Optional(CONF_RECORD_EVENTS): bool,
                            }
                        ),
                        user_input,
                    ),
                    errors={"base": "must_record_something"},
                )
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_BATCH_SIZE,
                        default=self.config_entry.options.get(
                            CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE
                        ),
                    ): int,
                    vol.Optional(
                        CONF_RECORD_STATES,
                        default=self.config_entry.options.get(
                            CONF_RECORD_STATES, DEFAULT_RECORD_STATES
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_RECORD_EVENTS,
                        default=self.config_entry.options.get(
                            CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS
                        ),
                    ): bool,
                }
            ),
        )

