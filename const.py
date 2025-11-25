"""Constants for the Chronicle integration."""

DOMAIN = "chronicle"

CONF_DB_URL = "db_url"
CONF_TABLE_NAME = "table_name"
CONF_CHUNK_TIME_INTERVAL = "chunk_time_interval"
CONF_COMPRESS_AFTER = "compress_after"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_EXCLUDE_ENTITIES = "exclude_entities"

DEFAULT_TABLE_NAME = "chronicle_events"
DEFAULT_CHUNK_TIME_INTERVAL = "7 days"
DEFAULT_COMPRESS_AFTER = "14 days"
