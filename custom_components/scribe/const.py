"""Constants for the Scribe integration."""

DOMAIN = "scribe"
CONF_DB_HOST = "db_host"
CONF_DB_PORT = "db_port"
CONF_DB_USER = "db_user"
CONF_DB_PASSWORD = "db_password"
CONF_DB_NAME = "db_name"
CONF_DB_URL = "db_url"
CONF_DB_SSL = "db_ssl"
CONF_CHUNK_TIME_INTERVAL = "chunk_time_interval"
CONF_COMPRESS_AFTER = "compress_after"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_EXCLUDE_ENTITIES = "exclude_entities"
CONF_EXCLUDE_ATTRIBUTES = "exclude_attributes"
CONF_RECORD_STATES = "record_states"
CONF_RECORD_EVENTS = "record_events"
CONF_BATCH_SIZE = "batch_size"
CONF_FLUSH_INTERVAL = "flush_interval"
CONF_MAX_QUEUE_SIZE = "max_queue_size"
CONF_TABLE_NAME_STATES = "table_name_states"
CONF_TABLE_NAME_EVENTS = "table_name_events"

# Statistics configuration
CONF_ENABLE_STATS_CHUNK = "enable_stats_chunk"
CONF_ENABLE_STATS_SIZE = "enable_stats_size"
CONF_ENABLE_STATS_IO = "enable_stats_io"
CONF_STATS_CHUNK_INTERVAL = "stats_chunk_interval"
CONF_STATS_SIZE_INTERVAL = "stats_size_interval"

CONF_BUFFER_ON_FAILURE = "buffer_on_failure"

DEFAULT_CHUNK_TIME_INTERVAL = "7 days"
DEFAULT_COMPRESS_AFTER = "60 days"
DEFAULT_DB_PORT = 5432
DEFAULT_DB_USER = "scribe"
DEFAULT_DB_NAME = "scribe"
DEFAULT_DB_SSL = False
DEFAULT_RECORD_STATES = True
DEFAULT_RECORD_EVENTS = False
DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 5
DEFAULT_MAX_QUEUE_SIZE = 10000
DEFAULT_TABLE_NAME_STATES = "states"
DEFAULT_TABLE_NAME_EVENTS = "events"

DEFAULT_ENABLE_STATS_CHUNK = False
DEFAULT_ENABLE_STATS_SIZE = False
DEFAULT_ENABLE_STATS_IO = False
DEFAULT_STATS_CHUNK_INTERVAL = 60  # minutes
DEFAULT_STATS_SIZE_INTERVAL = 60  # minutes

DEFAULT_BUFFER_ON_FAILURE = True