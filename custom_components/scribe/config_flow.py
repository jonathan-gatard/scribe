"""Config flow for Scribe integration.

This module handles the UI configuration for Scribe. It supports:
1. Initial setup via the Integrations page.
2. Import from configuration.yaml.
3. Options flow for re-configuring settings after setup.
"""
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_CHUNK_TIME_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_RECORD_STATES,
    CONF_RECORD_EVENTS,
    CONF_BATCH_SIZE,
    CONF_FLUSH_INTERVAL,
    CONF_MAX_QUEUE_SIZE,
    CONF_BUFFER_ON_FAILURE,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_BUFFER_ON_FAILURE,
)

_LOGGER = logging.getLogger(__name__)

class ScribeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scribe."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        # Handle Singleton: Set Unique ID
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors = {}

        if user_input is not None:
            # Validation
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(CONF_RECORD_EVENTS):
                errors["base"] = "must_record_something"
            else:
                try:
                    # Validate connection
                    await self.async_validate_input(user_input)
                    return self.async_create_entry(title="Scribe", data=user_input)
                except Exception as e:
                    _LOGGER.error("Connection error: %s", e)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DB_URL): str,
                    vol.Optional(
                        CONF_RECORD_STATES, default=DEFAULT_RECORD_STATES
                    ): bool,
                    vol.Optional(
                        CONF_RECORD_EVENTS, default=DEFAULT_RECORD_EVENTS
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from YAML."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates=user_input)
        return self.async_create_entry(title="Scribe", data=user_input)

    async def async_validate_input(self, data: dict) -> None:
        """Validate the user input allows us to connect."""
        _LOGGER.debug("Validating database connection...")
        db_url = data[CONF_DB_URL]
        
        # Ensure asyncpg driver
        if "postgresql://" in db_url and "postgresql+asyncpg://" not in db_url:
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
            data[CONF_DB_URL] = db_url

        try:
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy import text
            
            engine = create_async_engine(db_url)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()
        except Exception as e:
            _LOGGER.error(f"Database connection failed: {e}")
            raise Exception(f"Cannot connect to database: {e}")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ScribeOptionsFlowHandler(config_entry)

class ScribeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Scribe."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Manage the options."""
        errors = {}
        
        if user_input is not None:
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(CONF_RECORD_EVENTS):
                errors["base"] = "must_record_something"
            else:
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
                    ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=10000)),
                    vol.Optional(
                        CONF_FLUSH_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL
                        ),
                    ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=60, unit_of_measurement="seconds")),
                    vol.Optional(
                        CONF_RECORD_STATES,
                        default=self.config_entry.options.get(
                            CONF_RECORD_STATES, DEFAULT_RECORD_STATES
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_RECORD_EVENTS,
                        default=self.config_entry.options.get(
                            CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_BUFFER_ON_FAILURE,
                        default=self.config_entry.options.get(
                            CONF_BUFFER_ON_FAILURE, DEFAULT_BUFFER_ON_FAILURE
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_MAX_QUEUE_SIZE,
                        default=self.config_entry.options.get(
                            CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE
                        ),
                    ): selector.NumberSelector(selector.NumberSelectorConfig(min=100, max=100000, step=100)),
                    vol.Optional(
                        CONF_INCLUDE_DOMAINS,
                        default=self.config_entry.options.get(CONF_INCLUDE_DOMAINS, []),
                    ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_INCLUDE_ENTITIES,
                        default=self.config_entry.options.get(CONF_INCLUDE_ENTITIES, []),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_EXCLUDE_DOMAINS,
                        default=self.config_entry.options.get(CONF_EXCLUDE_DOMAINS, []),
                    ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_EXCLUDE_ENTITIES,
                        default=self.config_entry.options.get(CONF_EXCLUDE_ENTITIES, []),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_EXCLUDE_ATTRIBUTES,
                        default=self.config_entry.options.get(CONF_EXCLUDE_ATTRIBUTES, []),
                    ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
                }
            ),
            errors=errors,
        )
