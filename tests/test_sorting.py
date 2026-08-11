"""Tests for calculated route ordering."""

from dataclasses import dataclass

from smart_shopping_route.models import RouteSection, sort_items


@dataclass
class Item:
    uid: str
    summary: str
    status: str


SECTIONS = (RouteSection("Produce"), RouteSection("Bakery"), RouteSection("Dairy"))


def test_route_order_is_stable_and_unknown_is_last() -> None:
    items = [
        Item("1", "Milk", "open"),
        Item("2", "Apples", "open"),
        Item("3", "Bread", "open"),
        Item("4", "Bananas", "open"),
        Item("5", "Mystery", "open"),
    ]
    result = sort_items(
        items,
        SECTIONS,
        {"milk": "Dairy", "apples": "Produce", "bread": "Bakery", "bananas": "Produce"},
        show_completed=False,
        completed_status="done",
    )
    assert [item.uid for item in result] == ["2", "4", "3", "1", "5"]


def test_completed_hidden_or_sorted_after_incomplete() -> None:
    items = [Item("1", "Apples", "done"), Item("2", "Milk", "open")]
    classifications = {"apples": "Produce", "milk": "Dairy"}
    assert [
        item.uid
        for item in sort_items(
            items,
            SECTIONS,
            classifications,
            show_completed=False,
            completed_status="done",
        )
    ] == ["2"]
    assert [
        item.uid
        for item in sort_items(
            items,
            SECTIONS,
            classifications,
            show_completed=True,
            completed_status="done",
        )
    ] == ["2", "1"]


def test_reordering_sections_changes_order_without_classification_change() -> None:
    items = [Item("1", "Milk", "open"), Item("2", "Apples", "open")]
    classifications = {"milk": "Dairy", "apples": "Produce"}
    reordered = (RouteSection("Dairy"), RouteSection("Produce"))
    assert [
        item.uid
        for item in sort_items(
            items,
            reordered,
            classifications,
            show_completed=False,
            completed_status="done",
        )
    ] == ["1", "2"]
