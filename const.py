"""Constants for the Scribe integration."""

DOMAIN = "scribe"
CONF_DB_URL = "db_url"
CONF_CHUNK_TIME_INTERVAL = "chunk_time_interval"
CONF_COMPRESS_AFTER = "compress_after"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_EXCLUDE_ENTITIES = "exclude_entities"
CONF_RECORD_STATES = "record_states"
CONF_RECORD_EVENTS = "record_events"
CONF_BATCH_SIZE = "batch_size"
CONF_FLUSH_INTERVAL = "flush_interval"
CONF_TABLE_NAME_STATES = "table_name_states"
CONF_TABLE_NAME_EVENTS = "table_name_events"
CONF_DEBUG = "debug"
CONF_ENABLE_STATISTICS = "enable_statistics"

DEFAULT_CHUNK_TIME_INTERVAL = "7 days"
DEFAULT_COMPRESS_AFTER = "60 days"
DEFAULT_RECORD_STATES = True
DEFAULT_RECORD_EVENTS = False
DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 5
DEFAULT_TABLE_NAME_STATES = "states"
DEFAULT_TABLE_NAME_EVENTS = "events"
DEFAULT_DEBUG = False
DEFAULT_ENABLE_STATISTICS = False
