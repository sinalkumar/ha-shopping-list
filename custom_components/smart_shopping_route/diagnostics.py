"""Privacy-safe diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION
from .coordinator import RouteCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return counts and feature flags, never item text or identifiers."""
    coordinator: RouteCoordinator = hass.data[DOMAIN][entry.entry_id]
    ai_state = hass.states.get(entry.data["ai_task_entity_id"])
    return {
        "integration_version": VERSION,
        "section_count": len(coordinator.sections),
        "source_supported_features": coordinator.source.supported_features,
        "ai_task_supported_features": (
            int(ai_state.attributes.get("supported_features", 0)) if ai_state else 0
        ),
        "cache_entry_count": len(coordinator.cache.entries),
        "manual_override_count": coordinator.cache.manual_count,
        "uid_override_count": len(coordinator.cache.uid_overrides),
        "unclassified_count": coordinator.unclassified_count,
        "pending_count": len(coordinator.pending),
        "source_available": coordinator.source.available,
    }
