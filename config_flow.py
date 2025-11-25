"""Config flow for Scribe integration."""
import logging
import voluptuous as vol
from sqlalchemy import create_engine, text
from homeassistant import config_entries
from homeassistant.core import callback
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
    CONF_RECORD_STATES,
    CONF_RECORD_EVENTS,
    CONF_BATCH_SIZE,
    CONF_FLUSH_INTERVAL,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

class ScribeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scribe."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(CONF_RECORD_EVENTS):
                errors["base"] = "must_record_something"
            else:
                try:
                    await self.hass.async_add_executor_job(
                        self._validate_connection, user_input[CONF_DB_URL]
                    )
                    return self.async_create_entry(title="Scribe", data=user_input)
                except Exception as e:
                    _LOGGER.error("Connection error: %s", e)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DB_URL, default="postgresql://user:password@host:5432/db"): cv.string,
                    vol.Optional(CONF_CHUNK_TIME_INTERVAL, default=DEFAULT_CHUNK_TIME_INTERVAL): cv.string,
                    vol.Optional(CONF_COMPRESS_AFTER, default=DEFAULT_COMPRESS_AFTER): cv.string,
                    vol.Optional(CONF_RECORD_STATES, default=DEFAULT_RECORD_STATES): selector.BooleanSelector(),
                    vol.Optional(CONF_RECORD_EVENTS, default=DEFAULT_RECORD_EVENTS): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from YAML."""
        return await self.async_step_user(user_input)

    def _validate_connection(self, db_url):
        """Validate the database connection."""
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

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
                        CONF_CHUNK_TIME_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL
                        ),
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_COMPRESS_AFTER,
                        default=self.config_entry.options.get(
                            CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER
                        ),
                    ): selector.TextSelector(),
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
                }
            ),
        )
