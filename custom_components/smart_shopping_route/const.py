"""Constants for Smart Shopping Route."""

from __future__ import annotations

DOMAIN = "smart_shopping_route"
PLATFORMS = ["todo", "button", "sensor"]

CONF_PROFILE_NAME = "profile_name"
CONF_SOURCE_ENTITY_ID = "source_entity_id"
CONF_AI_TASK_ENTITY_ID = "ai_task_entity_id"
CONF_SECTIONS = "sections"
CONF_SHOW_COMPLETED = "show_completed"
CONF_DISPLAY_SECTION_PREFIX = "display_section_prefix"
CONF_PRICE_CHAIN = "price_chain"
CONF_PRICE_STORE_ID = "price_store_id"

DEFAULT_SHOW_COMPLETED = False
DEFAULT_DISPLAY_SECTION_PREFIX = False
DEFAULT_PRICE_CHAIN = "none"
FALLBACK_SECTION = "Unclassified / Other"

# Config entry data schema version. Bump together with config_flow.VERSION and
# add a migration branch in async_migrate_entry.
CONFIG_ENTRY_VERSION = 1
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.cache"
STORAGE_SAVE_DELAY = 5
MAX_CACHE_ENTRIES = 2000

# Prices refresh slowly and politely; the route never waits for them.
PRICE_REFRESH_HOURS = 12
PRICE_CONCURRENCY = 3
PRICE_FAILURE_BACKOFF_SECONDS = 3600

AI_TIMEOUT_SECONDS = 45
AI_CONCURRENCY = 3
# Answers below this confidence are treated as Unclassified / Other.
AI_MIN_CONFIDENCE = 0.5
# Failed or invalid AI responses are retried with capped exponential backoff so
# a provider outage cannot trigger a billable request on every list change.
AI_RETRY_BASE_SECONDS = 60
AI_RETRY_MAX_SECONDS = 3600

SERVICE_REFRESH = "refresh"
SERVICE_SET_ITEM_SECTION = "set_item_section"
SERVICE_FORGET_ITEM_SECTION = "forget_item_section"
SERVICE_RECLASSIFY_ALL = "reclassify_all"
SERVICE_CLEAR_CACHE = "clear_cache"

ATTR_ITEM_UID = "item_uid"
ATTR_SECTION = "section"
ATTR_REMEMBER = "remember"
ATTR_INCLUDE_MANUAL = "include_manual"
