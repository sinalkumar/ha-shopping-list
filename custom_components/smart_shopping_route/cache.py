"""Persistent, bounded classification cache."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .const import MAX_CACHE_ENTRIES
from .models import normalize_summary


@dataclass(slots=True)
class Classification:
    """A remembered classification."""

    section: str
    manual: bool
    last_used: float


class ClassificationCache:
    """A small per-profile LRU cache with separate per-UID overrides.

    The cache is the single source of truth for classifications: a remembered
    manual correction and a UID-only correction live here, never in the
    coordinator, so that clearing or pruning always sees both.
    """

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max_entries
        self.entries: dict[str, Classification] = {}
        self.uid_overrides: dict[str, str] = {}

    def get(self, summary: str, uid: str | None = None) -> str | None:
        if uid and uid in self.uid_overrides:
            return self.uid_overrides[uid]
        key = normalize_summary(summary)
        entry = self.entries.get(key)
        if entry:
            entry.last_used = time.time()
            return entry.section
        return None

    def set(self, summary: str, section: str, *, manual: bool = False) -> None:
        key = normalize_summary(summary)
        if not key:
            return
        existing = self.entries.get(key)
        if existing and existing.manual and not manual:
            return
        self.entries[key] = Classification(section, manual, time.time())
        self._trim()

    def set_uid(self, uid: str, section: str) -> None:
        self.uid_overrides[uid] = section

    def forget_uid(self, uid: str) -> bool:
        """Drop a UID-only correction; return whether anything changed."""

        return self.uid_overrides.pop(uid, None) is not None

    def forget(self, summary: str, uid: str | None = None) -> None:
        self.entries.pop(normalize_summary(summary), None)
        if uid:
            self.uid_overrides.pop(uid, None)

    def prune_uids(self, valid_uids: Iterable[str]) -> bool:
        """Drop overrides for items that no longer exist in the source list."""

        valid = set(valid_uids)
        stale = [uid for uid in self.uid_overrides if uid not in valid]
        for uid in stale:
            del self.uid_overrides[uid]
        return bool(stale)

    def clear_ai(self) -> None:
        self.entries = {
            key: value for key, value in self.entries.items() if value.manual
        }

    def clear(self, *, include_manual: bool = False) -> None:
        if include_manual:
            self.entries.clear()
            self.uid_overrides.clear()
        else:
            self.clear_ai()

    def invalidate_sections(self, valid_sections: set[str]) -> None:
        valid = {value.casefold() for value in valid_sections}
        self.entries = {
            key: entry
            for key, entry in self.entries.items()
            if entry.section.casefold() in valid
        }
        self.uid_overrides = {
            uid: section
            for uid, section in self.uid_overrides.items()
            if section.casefold() in valid
        }

    @property
    def manual_count(self) -> int:
        return sum(entry.manual for entry in self.entries.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": {
                key: {
                    "section": entry.section,
                    "manual": entry.manual,
                    "last_used": entry.last_used,
                }
                for key, entry in self.entries.items()
            },
            "uid_overrides": dict(self.uid_overrides),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], max_entries: int = MAX_CACHE_ENTRIES
    ) -> ClassificationCache:
        cache = cls(max_entries)
        for key, raw in data.get("entries", {}).items():
            try:
                cache.entries[key] = Classification(
                    str(raw["section"]),
                    bool(raw.get("manual", False)),
                    float(raw.get("last_used", 0)),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        overrides = data.get("uid_overrides", {})
        if isinstance(overrides, dict):
            cache.uid_overrides = {
                str(uid): str(section) for uid, section in overrides.items()
            }
        cache._trim()
        return cache

    def _trim(self) -> None:
        excess = len(self.entries) - self.max_entries
        if excess <= 0:
            return
        removable = sorted(
            ((key, entry) for key, entry in self.entries.items() if not entry.manual),
            key=lambda item: item[1].last_used,
        )
        for key, _entry in removable[:excess]:
            self.entries.pop(key, None)
