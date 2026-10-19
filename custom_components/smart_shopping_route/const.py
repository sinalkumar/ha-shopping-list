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
PRICE_STORAGE_KEY_PREFIX = f"{DOMAIN}.prices"
STORAGE_SAVE_DELAY = 5
MAX_CACHE_ENTRIES = 2000

# Prices refresh slowly and politely; the route never waits for them.
PRICE_REFRESH_HOURS = 12
# Both chains publish the new week's prices between Sunday morning and Monday
# 05:00 NZ time: 93% of Pak'nSave and 98% of Woolworths price changes observed in
# 2026 land on a Monday, and grocer.nz records them at 05:00. The interval alone
# can be half a day late on that one morning, which is a whole shopping trip, so
# the list is also re-priced shortly after the rollover.
PRICE_WEEKLY_REFRESH_WEEKDAY = 0  # Monday, as datetime.weekday() reports it
PRICE_WEEKLY_REFRESH_HOUR = 6
PRICE_CONCURRENCY = 3
PRICE_FAILURE_BACKOFF_SECONDS = 3600
# Price history is persisted, so it must be bounded like the classification cache.
PRICE_HISTORY_MAX = 500
# Observations kept per item. Changes are always recorded; unchanged readings are
# sampled so a long-running profile keeps a usable series without growing.
PRICE_HISTORY_MAX_POINTS = 60
PRICE_HISTORY_MIN_INTERVAL = 6 * 3600

# grocer.nz mirror. History is the only source of Pak'nSave reference prices, so
# it is kept on disk and refreshed slowly rather than fetched on demand.
GROCER_MIRROR_DIR = f".storage/{DOMAIN}/grocer"
GROCER_CATALOGUE_REFRESH_HOURS = 24 * 7
GROCER_STORE_REFRESH_HOURS = 12
# Bound the mirror so history is kept indefinitely without unbounded growth.
GROCER_HISTORY_FILE_MAX = 400
CONF_GROCER_STORE_ID = "grocer_store_id"
# Pins outlive their item by design, so they need their own ceiling.
PRICE_PIN_MAX = 200

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
SERVICE_PIN_ITEM_PRODUCT = "pin_item_product"
SERVICE_UNPIN_ITEM_PRODUCT = "unpin_item_product"

ATTR_ITEM_UID = "item_uid"
ATTR_SECTION = "section"
ATTR_REMEMBER = "remember"
ATTR_INCLUDE_MANUAL = "include_manual"
ATTR_PRODUCT_ID = "product_id"
ATTR_QUERY = "query"
ATTR_CHAIN = "chain"
ATTR_GROCER_PRODUCT_ID = "grocer_product_id"
