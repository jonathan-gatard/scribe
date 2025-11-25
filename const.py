"""Constants for the Scribe integration."""

DOMAIN = "scribe"

CONF_DB_URL = "db_url"
CONF_TABLE_NAME = "table_name" # Legacy, kept for compatibility or single-table mode if needed
CONF_CHUNK_TIME_INTERVAL = "chunk_time_interval"
CONF_COMPRESS_AFTER = "compress_after"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_EXCLUDE_ENTITIES = "exclude_entities"

CONF_RECORD_STATES = "record_states"
CONF_RECORD_EVENTS = "record_events"

DEFAULT_CHUNK_TIME_INTERVAL = "30 days"
DEFAULT_COMPRESS_AFTER = "7 days"
DEFAULT_RECORD_STATES = True
DEFAULT_RECORD_EVENTS = False
