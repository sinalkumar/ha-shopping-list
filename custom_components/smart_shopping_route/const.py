"""Constants for Smart Shopping Route."""

from __future__ import annotations

DOMAIN = "smart_shopping_route"
VERSION = "0.1.0"
PLATFORMS = ["todo", "button", "sensor"]

CONF_PROFILE_NAME = "profile_name"
CONF_SOURCE_ENTITY_ID = "source_entity_id"
CONF_AI_TASK_ENTITY_ID = "ai_task_entity_id"
CONF_SECTIONS = "sections"
CONF_SHOW_COMPLETED = "show_completed"
CONF_DISPLAY_SECTION_PREFIX = "display_section_prefix"

DEFAULT_SHOW_COMPLETED = False
DEFAULT_DISPLAY_SECTION_PREFIX = False
FALLBACK_SECTION = "Unclassified / Other"
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.cache"
MAX_CACHE_ENTRIES = 2000
AI_TIMEOUT_SECONDS = 45
AI_CONCURRENCY = 3

SERVICE_REFRESH = "refresh"
SERVICE_SET_ITEM_SECTION = "set_item_section"
SERVICE_FORGET_ITEM_SECTION = "forget_item_section"
SERVICE_RECLASSIFY_ALL = "reclassify_all"
SERVICE_CLEAR_CACHE = "clear_cache"

ATTR_ROUTE_ENTITY_ID = "route_entity_id"
ATTR_ITEM_UID = "item_uid"
ATTR_ITEM = "item"
ATTR_SECTION = "section"
ATTR_REMEMBER = "remember"
ATTR_INCLUDE_MANUAL = "include_manual"
