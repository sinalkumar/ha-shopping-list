"""UID-specific corrections must keep duplicate summaries independent."""

from dataclasses import dataclass

from smart_shopping_route.models import RouteSection, sort_items


@dataclass
class Item:
    uid: str
    summary: str
    status: str


def test_duplicate_summaries_can_have_uid_specific_sections() -> None:
    items = [Item("uid-1", "Milk", "open"), Item("uid-2", "Milk", "open")]
    classifications = {
        "milk": "Dairy",
        "uid:uid-2": "Produce",
        "uid:uid-1": "Dairy",
    }
    result = sort_items(
        items,
        (RouteSection("Produce"), RouteSection("Dairy")),
        classifications,
        show_completed=False,
        completed_status="done",
    )
    assert [item.uid for item in result] == ["uid-2", "uid-1"]
