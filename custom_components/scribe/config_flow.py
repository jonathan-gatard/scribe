"""Config flow for Scribe integration.

This module handles the UI configuration for Scribe. It supports:
1. Initial setup via the Integrations page.
2. Import from configuration.yaml.
3. Options flow for re-configuring settings after setup.
"""
import logging
import voluptuous as vol
from sqlalchemy import create_engine, text
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
    CONF_ENABLE_STATISTICS,
    CONF_MAX_QUEUE_SIZE,
    CONF_BUFFER_ON_FAILURE,
    DEFAULT_CHUNK_TIME_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    DEFAULT_RECORD_STATES,
    DEFAULT_RECORD_EVENTS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_ENABLE_STATISTICS,
    DEFAULT_BUFFER_ON_FAILURE,
)

_LOGGER = logging.getLogger(__name__)

class ScribeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scribe.
    
    The config flow is responsible for creating the ConfigEntry.
    It validates the database connection.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step.
        
        Displays the form to the user and processes the input.
        """
        # Only allow one instance of Scribe
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        
        errors = {}

        if user_input is not None:
            # Validation: Must record at least states or events
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(CONF_RECORD_EVENTS):
                errors["base"] = "must_record_something"
            else:
                try:
                    # Validate connection in executor to avoid blocking loop
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
                vol.Required(CONF_DB_URL): str,
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
        """Handle import from YAML.
        
        Triggered by async_setup in __init__.py if configuration.yaml contains 'scribe:'.
        If an instance already exists, we update it with the YAML config (YAML takes precedence).
        """
        # Check if an instance already exists
        existing_entries = self._async_current_entries()
        if existing_entries:
            # Update the existing entry with YAML config
            existing_entry = existing_entries[0]
            _LOGGER.info("Scribe config entry already exists, updating with YAML config")
            self.hass.config_entries.async_update_entry(
                existing_entry,
                data=user_input
            )
            # Reload the entry to apply new config
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="already_configured")
        
        return await self.async_step_user(user_input)

    @staticmethod
    def validate_input(hass: HomeAssistant, data: dict) -> dict:
        """Validate the user input allows us to connect.
        
        This method attempts to connect to the target database.
        """
        from .const import CONF_DB_URL
        
        db_url = data[CONF_DB_URL]
        
        # Replace postgresql:// with postgresql+asyncpg:// for validation if needed, 
        # but for simple validation we can stick to sync engine or just check if it's a valid string.
        # However, since we are moving to asyncpg, the actual runtime will use asyncpg.
        # For validation here, we can still use sync psycopg2 if installed, or just try to connect.
        # Given requirements, we should probably stick to sync validation for simplicity here 
        # OR switch to async validation. Let's stick to sync validation for now as it runs in executor.
        
        try:
            # We might need to ensure the URL is compatible with the sync driver for this check
            # If the user provides postgresql+asyncpg://, create_engine might fail if asyncpg is not installed in the sync context?
            # Actually, we added asyncpg to requirements.
            # But create_engine (sync) won't work with asyncpg driver.
            # So we should strip +asyncpg if present for this check, or ensure we use a sync driver.
            
            sync_url = db_url.replace("+asyncpg", "")
            engine = create_engine(sync_url)
            with engine.connect() as conn:
                pass
        except Exception as e:
            _LOGGER.error(f"Database connection failed: {e}")
            raise Exception(f"Cannot connect to database: {e}")

        return {"title": "Scribe"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ScribeOptionsFlowHandler(config_entry)

class ScribeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Scribe.
    
    Allows users to change settings without restarting Home Assistant.
    Supports filtering (include/exclude) and performance tuning (batch size, flush interval).
    """

    def __init__(self, config_entry):
        """Initialize options flow."""
        # self.config_entry is set by the parent class in recent HA versions
        pass

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
