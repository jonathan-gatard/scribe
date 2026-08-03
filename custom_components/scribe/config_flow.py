"""Config flow for Scribe integration.

Supports both UI configuration (async_step_user) and YAML import (async_step_import).
YAML config takes priority: if a config entry already exists it is updated in-place.

Options flow is split into 5 steps:
  init       → Recording & filtering
  performance → Batch / flush / queue / buffer
  stats      → Statistics sensors
  metadata   → Metadata tables (areas, devices, entities…)
  advanced   → TimescaleDB tuning + SSL
"""

import logging

import asyncpg
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_DB_URL,
    CONF_DB_SSL,
    DEFAULT_DB_SSL,
    CONF_SSL_ROOT_CERT,
    CONF_SSL_CERT_FILE,
    CONF_SSL_KEY_FILE,
    CONF_CHUNK_TIME_INTERVAL,
    DEFAULT_CHUNK_TIME_INTERVAL,
    CONF_COMPRESS_AFTER,
    DEFAULT_COMPRESS_AFTER,
    CONF_RECORD_STATES,
    DEFAULT_RECORD_STATES,
    CONF_RECORD_EVENTS,
    DEFAULT_RECORD_EVENTS,
    CONF_BATCH_SIZE,
    DEFAULT_BATCH_SIZE,
    CONF_FLUSH_INTERVAL,
    DEFAULT_FLUSH_INTERVAL,
    CONF_MAX_QUEUE_SIZE,
    DEFAULT_MAX_QUEUE_SIZE,
    CONF_BUFFER_ON_FAILURE,
    DEFAULT_BUFFER_ON_FAILURE,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_EXCLUDE_ATTRIBUTES,
    CONF_INCLUDE_EVENTS,
    CONF_EXCLUDE_EVENTS,
    CONF_ENABLE_STATS_IO,
    DEFAULT_ENABLE_STATS_IO,
    CONF_ENABLE_STATS_CHUNK,
    DEFAULT_ENABLE_STATS_CHUNK,
    CONF_ENABLE_STATS_SIZE,
    DEFAULT_ENABLE_STATS_SIZE,
    CONF_STATS_CHUNK_INTERVAL,
    DEFAULT_STATS_CHUNK_INTERVAL,
    CONF_STATS_SIZE_INTERVAL,
    DEFAULT_STATS_SIZE_INTERVAL,
    CONF_ENABLE_AREAS,
    DEFAULT_ENABLE_AREAS,
    CONF_ENABLE_DEVICES,
    DEFAULT_ENABLE_DEVICES,
    CONF_ENABLE_INTEGRATIONS,
    DEFAULT_ENABLE_INTEGRATIONS,
    CONF_ENABLE_USERS,
    DEFAULT_ENABLE_USERS,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_dsn(db_url: str) -> str:
    """Strip SQLAlchemy dialect prefix if present."""
    return db_url.replace("postgresql+asyncpg://", "postgresql://")


async def _test_connection(db_url: str) -> bool:
    """Attempt a real connection to validate the database URL."""
    try:
        conn = await asyncpg.connect(dsn=_normalize_dsn(db_url), timeout=5)
        await conn.close()
        return True
    except Exception as e:
        _LOGGER.debug("Connection test failed: %s", e)
        return False


def _multi_text_selector() -> selector.SelectSelector:
    """Free-text multi-value selector (for globs, attributes…)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[],
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _domain_selector(hass) -> selector.SelectSelector:
    """Domain selector built from domains currently present in the HA instance."""
    domains = sorted({state.domain for state in hass.states.async_all()})
    options = [{"value": d, "label": d} for d in domains]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _coerce_options(data: dict) -> dict:
    """Convert NumberSelector floats to ints and ensure lists where expected."""
    result = dict(data)
    for key in (
        CONF_BATCH_SIZE,
        CONF_FLUSH_INTERVAL,
        CONF_MAX_QUEUE_SIZE,
        CONF_STATS_CHUNK_INTERVAL,
        CONF_STATS_SIZE_INTERVAL,
    ):
        if key in result:
            result[key] = int(result[key])
    for key in (
        CONF_INCLUDE_DOMAINS,
        CONF_EXCLUDE_DOMAINS,
        CONF_INCLUDE_ENTITIES,
        CONF_EXCLUDE_ENTITIES,
        CONF_INCLUDE_ENTITY_GLOBS,
        CONF_EXCLUDE_ENTITY_GLOBS,
        CONF_EXCLUDE_ATTRIBUTES,
        CONF_INCLUDE_EVENTS,
        CONF_EXCLUDE_EVENTS,
    ):
        if key in result and not isinstance(result[key], list):
            result[key] = [result[key]] if result[key] else []
    return result


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class ScribeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scribe."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: ask for the database URL and test the connection."""
        errors = {}

        if user_input is not None:
            db_url = user_input.get(CONF_DB_URL, "").strip()
            if not db_url:
                errors[CONF_DB_URL] = "cannot_connect"
            elif not await _test_connection(db_url):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured(updates={CONF_DB_URL: db_url})
                return self.async_create_entry(
                    title="Scribe",
                    data={CONF_DB_URL: db_url},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DB_URL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from configuration.yaml (YAML-first setup)."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates=user_input)
        return self.async_create_entry(title="Scribe", data=user_input or {})

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return ScribeOptionsFlowHandler()


# ---------------------------------------------------------------------------
# Options flow  (5 steps)
# ---------------------------------------------------------------------------


class ScribeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Scribe runtime options (editable after initial setup)."""

    def __init__(self):
        """Initialise temporary options accumulator."""
        self._options: dict = {}

    # ------------------------------------------------------------------
    # Step 1 – Recording & filtering
    # ------------------------------------------------------------------
    async def async_step_init(self, user_input=None) -> FlowResult:
        """Recording options and entity filters."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            if not user_input.get(CONF_RECORD_STATES) and not user_input.get(
                CONF_RECORD_EVENTS
            ):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema_init(current),
                    errors={"base": "must_record_something"},
                )
            self._options.update(user_input)
            return await self.async_step_performance()

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema_init(current),
        )

    def _schema_init(self, c: dict) -> vol.Schema:
        def g(k, d):
            return c.get(k, d)

        _ds = _domain_selector(self.hass)
        return vol.Schema(
            {
                vol.Optional(
                    CONF_RECORD_STATES,
                    default=g(CONF_RECORD_STATES, DEFAULT_RECORD_STATES),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_RECORD_EVENTS,
                    default=g(CONF_RECORD_EVENTS, DEFAULT_RECORD_EVENTS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_INCLUDE_DOMAINS, default=g(CONF_INCLUDE_DOMAINS, [])
                ): _ds,
                vol.Optional(
                    CONF_EXCLUDE_DOMAINS, default=g(CONF_EXCLUDE_DOMAINS, [])
                ): _ds,
                vol.Optional(
                    CONF_INCLUDE_ENTITIES, default=g(CONF_INCLUDE_ENTITIES, [])
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Optional(
                    CONF_EXCLUDE_ENTITIES, default=g(CONF_EXCLUDE_ENTITIES, [])
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Optional(
                    CONF_INCLUDE_ENTITY_GLOBS, default=g(CONF_INCLUDE_ENTITY_GLOBS, [])
                ): _multi_text_selector(),
                vol.Optional(
                    CONF_EXCLUDE_ENTITY_GLOBS, default=g(CONF_EXCLUDE_ENTITY_GLOBS, [])
                ): _multi_text_selector(),
                vol.Optional(
                    CONF_EXCLUDE_ATTRIBUTES, default=g(CONF_EXCLUDE_ATTRIBUTES, [])
                ): _multi_text_selector(),
                vol.Optional(
                    CONF_INCLUDE_EVENTS, default=g(CONF_INCLUDE_EVENTS, [])
                ): _multi_text_selector(),
                vol.Optional(
                    CONF_EXCLUDE_EVENTS, default=g(CONF_EXCLUDE_EVENTS, [])
                ): _multi_text_selector(),
            }
        )

    # ------------------------------------------------------------------
    # Step 2 – Performance
    # ------------------------------------------------------------------
    async def async_step_performance(self, user_input=None) -> FlowResult:
        """Batch size, flush interval, queue and buffer settings."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_stats()

        return self.async_show_form(
            step_id="performance",
            data_schema=self._schema_performance(current),
        )

    def _schema_performance(self, c: dict) -> vol.Schema:
        def g(k, d):
            return c.get(k, d)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_BATCH_SIZE, default=g(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=10000,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_FLUSH_INTERVAL,
                    default=g(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=300,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_MAX_QUEUE_SIZE,
                    default=g(CONF_MAX_QUEUE_SIZE, DEFAULT_MAX_QUEUE_SIZE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=100,
                        max=100000,
                        step=100,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_BUFFER_ON_FAILURE,
                    default=g(CONF_BUFFER_ON_FAILURE, DEFAULT_BUFFER_ON_FAILURE),
                ): selector.BooleanSelector(),
            }
        )

    # ------------------------------------------------------------------
    # Step 3 – Statistics sensors
    # ------------------------------------------------------------------
    async def async_step_stats(self, user_input=None) -> FlowResult:
        """Enable/configure statistics sensors."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_metadata()

        return self.async_show_form(
            step_id="stats",
            data_schema=self._schema_stats(current),
        )

    def _schema_stats(self, c: dict) -> vol.Schema:
        def g(k, d):
            return c.get(k, d)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_STATS_IO,
                    default=g(CONF_ENABLE_STATS_IO, DEFAULT_ENABLE_STATS_IO),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_STATS_CHUNK,
                    default=g(CONF_ENABLE_STATS_CHUNK, DEFAULT_ENABLE_STATS_CHUNK),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_STATS_CHUNK_INTERVAL,
                    default=g(CONF_STATS_CHUNK_INTERVAL, DEFAULT_STATS_CHUNK_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=1440,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Optional(
                    CONF_ENABLE_STATS_SIZE,
                    default=g(CONF_ENABLE_STATS_SIZE, DEFAULT_ENABLE_STATS_SIZE),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_STATS_SIZE_INTERVAL,
                    default=g(CONF_STATS_SIZE_INTERVAL, DEFAULT_STATS_SIZE_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=1440,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
            }
        )

    # ------------------------------------------------------------------
    # Step 4 – Metadata tables
    # ------------------------------------------------------------------
    async def async_step_metadata(self, user_input=None) -> FlowResult:
        """Enable/disable metadata tables."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_advanced()

        return self.async_show_form(
            step_id="metadata",
            data_schema=self._schema_metadata(current),
        )

    def _schema_metadata(self, c: dict) -> vol.Schema:
        def g(k, d):
            return c.get(k, d)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_AREAS,
                    default=g(CONF_ENABLE_AREAS, DEFAULT_ENABLE_AREAS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_DEVICES,
                    default=g(CONF_ENABLE_DEVICES, DEFAULT_ENABLE_DEVICES),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_INTEGRATIONS,
                    default=g(CONF_ENABLE_INTEGRATIONS, DEFAULT_ENABLE_INTEGRATIONS),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_USERS,
                    default=g(CONF_ENABLE_USERS, DEFAULT_ENABLE_USERS),
                ): selector.BooleanSelector(),
            }
        )

    # ------------------------------------------------------------------
    # Step 5 – Advanced (TimescaleDB + SSL)
    # ------------------------------------------------------------------
    async def async_step_advanced(self, user_input=None) -> FlowResult:
        """TimescaleDB tuning and SSL configuration."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(
                title="", data=_coerce_options(self._options)
            )

        return self.async_show_form(
            step_id="advanced",
            data_schema=self._schema_advanced(current),
        )

    def _schema_advanced(self, c: dict) -> vol.Schema:
        def g(k, d):
            return c.get(k, d)

        return vol.Schema(
            {
                vol.Optional(
                    CONF_CHUNK_TIME_INTERVAL,
                    default=g(CONF_CHUNK_TIME_INTERVAL, DEFAULT_CHUNK_TIME_INTERVAL),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_COMPRESS_AFTER,
                    default=g(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_DB_SSL, default=g(CONF_DB_SSL, DEFAULT_DB_SSL)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_SSL_ROOT_CERT, default=g(CONF_SSL_ROOT_CERT, "")
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_SSL_CERT_FILE, default=g(CONF_SSL_CERT_FILE, "")
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_SSL_KEY_FILE, default=g(CONF_SSL_KEY_FILE, "")
                ): selector.TextSelector(),
            }
        )
