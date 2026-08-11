"""Tests for route parsing and normalization."""

import pytest

from smart_shopping_route.models import (
    RouteParseError,
    RouteSection,
    normalize_summary,
    parse_sections,
    serialize_sections,
)


def test_parse_sections_preserves_order_and_optional_hints() -> None:
    sections = parse_sections(" Produce | fruit, herbs \n\n Bakery \n Dairy | milk ")
    assert sections == (
        RouteSection("Produce", "fruit, herbs"),
        RouteSection("Bakery", ""),
        RouteSection("Dairy", "milk"),
    )
    assert parse_sections(serialize_sections(sections)) == sections


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "no_sections"),
        (" | hints", "empty_section"),
        ("A | x | y", "malformed_sections"),
        ("Produce\nproduce | fruit", "duplicate_section"),
        ("Unclassified / Other", "reserved_section"),
    ],
)
def test_invalid_routes(value: str, code: str) -> None:
    with pytest.raises(RouteParseError, match=code):
        parse_sections(value)


def test_normalize_summary_is_unicode_casefold_and_whitespace_safe() -> None:
    assert normalize_summary("  MILK\t  2L  ") == "milk 2l"
    assert normalize_summary("Ｃａｆé") == "café"
    assert normalize_summary("Milk 2L") != normalize_summary("Milk 1L")
