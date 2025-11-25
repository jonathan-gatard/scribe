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
    CONF_DB_HOST,
    CONF_DB_PORT,
    CONF_DB_USER,
    CONF_DB_PASSWORD,
    CONF_DB_NAME,
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
    CONF_ENABLE_STATISTICS,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_ENABLE_STATISTICS,
    DEFAULT_DB_PORT,
    DEFAULT_DB_USER,
    DEFAULT_DB_NAME,
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
                        self.validate_input, self.hass, user_input
                    )
                    return self.async_create_entry(title="Scribe", data=user_input)
                except Exception as e:
                    _LOGGER.error("Connection error: %s", e)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema = vol.Schema(
            {
                vol.Required(CONF_DB_HOST): str,
                vol.Required(CONF_DB_PORT, default=DEFAULT_DB_PORT): int,
                vol.Required(CONF_DB_USER, default=DEFAULT_DB_USER): str,
                vol.Required(CONF_DB_PASSWORD): str,
                vol.Required(CONF_DB_NAME, default=DEFAULT_DB_NAME): str,
                vol.Optional(
                    CONF_RECORD_STATES, default=DEFAULT_RECORD_STATES
                ): bool,
                    vol.Optional(
                        CONF_RECORD_EVENTS, default=DEFAULT_RECORD_EVENTS
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_STATISTICS, default=DEFAULT_ENABLE_STATISTICS
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from YAML."""
        return await self.async_step_user(user_input)

    @staticmethod
    def validate_input(hass: HomeAssistant, data: dict) -> dict:
        """Validate the user input allows us to connect.

        Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
        """
        db_url = f"postgresql://{data[CONF_DB_USER]}:{data[CONF_DB_PASSWORD]}@{data[CONF_DB_HOST]}:{data[CONF_DB_PORT]}/{data[CONF_DB_NAME]}"
        
        try:
            engine = create_engine(db_url)
            with engine.connect() as conn:
                pass
        except Exception:
            # Try to connect to postgres db to create the target db
            postgres_url = f"postgresql://{data[CONF_DB_USER]}:{data[CONF_DB_PASSWORD]}@{data[CONF_DB_HOST]}:{data[CONF_DB_PORT]}/postgres"
            try:
                engine = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
                with engine.connect() as conn:
                    # Check if db exists
                    res = conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname = '{data[CONF_DB_NAME]}'"))
                    if not res.fetchone():
                        conn.execute(text(f"CREATE DATABASE {data[CONF_DB_NAME]}"))
                
                # Verify connection to new db
                engine = create_engine(db_url)
                with engine.connect() as conn:
                    pass
                    
            except Exception as e:
                _LOGGER.error(f"Database connection failed: {e}")
                raise InvalidAuth

        return {"title": "Scribe"}

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
                        CONF_ENABLE_STATISTICS,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_STATISTICS, DEFAULT_ENABLE_STATISTICS
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
            errors=errors,
        )
