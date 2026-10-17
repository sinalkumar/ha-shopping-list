"""Shared device registry metadata for one route profile."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import CONF_PROFILE_NAME, DOMAIN


def route_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Group every entity of a profile under one service device."""

    name = entry.data.get(CONF_PROFILE_NAME) or entry.title
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=name,
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Smart Shopping Route",
        model="Calculated shopping route",
    )
