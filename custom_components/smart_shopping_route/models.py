"""Data models and deterministic route logic."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .const import FALLBACK_SECTION


@dataclass(frozen=True, slots=True)
class RouteSection:
    """One stop in a physical shopping route."""

    name: str
    hints: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RouteSection:
        """Build a section from config entry data, ignoring unknown keys.

        Config entry data is persisted user data, so a future or partially
        written key must never make the whole profile fail to load.
        """

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("section_name_missing")
        hints = raw.get("hints", "")
        return cls(name.strip(), hints.strip() if isinstance(hints, str) else "")


class ItemLike(Protocol):
    """The TodoItem fields used by the pure ordering implementation."""

    uid: str | None
    summary: str | None
    status: object | None


class RouteParseError(ValueError):
    """Raised when route text is invalid."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_summary(value: str) -> str:
    """Normalize an item summary without merging quantities or products."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def parse_sections(value: str) -> tuple[RouteSection, ...]:
    """Parse the UI's line-oriented route definition."""

    sections: list[RouteSection] = []
    seen: set[str] = set()
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.count("|") > 1:
            raise RouteParseError("malformed_sections")
        name, separator, hints = line.partition("|")
        name = name.strip()
        hints = hints.strip() if separator else ""
        if not name:
            raise RouteParseError("empty_section")
        normalized = name.casefold()
        if normalized == FALLBACK_SECTION.casefold():
            raise RouteParseError("reserved_section")
        if normalized in seen:
            raise RouteParseError("duplicate_section")
        seen.add(normalized)
        sections.append(RouteSection(name, hints))
    if not sections:
        raise RouteParseError("no_sections")
    return tuple(sections)


def serialize_sections(sections: Iterable[RouteSection]) -> str:
    """Serialize route sections for config entry storage/UI defaults."""

    return "\n".join(
        f"{section.name} | {section.hints}" if section.hints else section.name
        for section in sections
    )


def section_names(sections: Iterable[RouteSection]) -> tuple[str, ...]:
    """Return configured names plus the internal fallback."""

    return (*(section.name for section in sections), FALLBACK_SECTION)


def sort_items[TItem: ItemLike](
    items: Iterable[TItem],
    sections: Iterable[RouteSection],
    classifications: Mapping[str, str],
    *,
    show_completed: bool,
    completed_status: object,
) -> list[TItem]:
    """Sort items without changing stable order within a section."""

    section_order = {
        name.casefold(): index for index, name in enumerate(section_names(sections))
    }
    fallback_index = len(section_order) - 1
    visible = [
        item for item in items if show_completed or item.status != completed_status
    ]
    return sorted(
        visible,
        key=lambda item: (
            item.status == completed_status,
            section_order.get(
                (
                    classifications.get(f"uid:{item.uid}")
                    or classifications.get(normalize_summary(item.summary or ""), "")
                ).casefold(),
                fallback_index,
            ),
        ),
    )


def decorate_summary(summary: str, section: str, enabled: bool) -> str:
    """Decorate only the proxy representation."""

    return f"{section} · {summary}" if enabled else summary


def strip_display_prefix(summary: str, known_sections: Iterable[str]) -> str:
    """Remove exactly one integration-created prefix from an edited summary."""

    for section in known_sections:
        prefix = f"{section} · "
        if summary.startswith(prefix):
            return summary.removeprefix(prefix).strip()
    return summary
