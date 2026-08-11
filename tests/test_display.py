"""Tests for safe display-only section decoration."""

from smart_shopping_route.models import decorate_summary, strip_display_prefix


def test_prefix_round_trip_does_not_change_plain_source_text() -> None:
    decorated = decorate_summary("Milk", "Dairy", True)
    assert decorated == "Dairy · Milk"
    assert strip_display_prefix(decorated, ("Produce", "Dairy")) == "Milk"
    assert strip_display_prefix("Milk", ("Produce", "Dairy")) == "Milk"


def test_user_text_is_only_stripped_for_an_exact_known_prefix() -> None:
    assert strip_display_prefix("Unknown · Milk", ("Dairy",)) == "Unknown · Milk"
