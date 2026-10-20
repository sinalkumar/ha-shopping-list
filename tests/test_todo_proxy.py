"""Route Todo entity: prefix gating, feature masking, and device grouping."""

import asyncio
from hashlib import sha256
from types import SimpleNamespace

import pytest
from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature
from homeassistant.exceptions import ServiceValidationError

import smart_shopping_route.todo as todo_module
from smart_shopping_route.models import RouteSection
from smart_shopping_route.prices import PriceQuote
from smart_shopping_route.todo import SmartShoppingRouteTodo


class Source:
    entity_id = "todo.synthetic_source"
    supported_features = int(
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
    )

    def __init__(self) -> None:
        self.created: list[TodoItem] = []
        self.updated: list[TodoItem] = []
        self.deleted: list[list[str]] = []

    async def async_create(self, item: TodoItem) -> None:
        self.created.append(item)

    async def async_update(self, item: TodoItem) -> None:
        self.updated.append(item)

    async def async_delete(self, uids: list[str]) -> None:
        self.deleted.append(uids)


class FakePrices:
    """Price stand-in: disabled unless a test overrides it."""

    enabled = False
    chain = "none"

    def __init__(self, quotes=None, failed=None, alternatives=None) -> None:
        self._quotes = quotes or {}
        self.failed = set(failed or ())
        self._alternatives = alternatives or {}
        self.enabled = bool(self._quotes)

    def quote_for(self, summary):
        return self._quotes.get(summary)

    def alternatives_for(self, summary):
        return self._alternatives.get(summary, [])

    @property
    def snapshot(self):
        return SimpleNamespace(
            reference_point=lambda _key: None,
            stale_for=lambda _key: False,
            failed=self.failed,
        )


class Coordinator:
    available = True
    sections = (RouteSection("Produce"), RouteSection("Dairy"))

    def __init__(self) -> None:
        self.source = Source()
        self.prices = FakePrices()
        self.items = [
            TodoItem(uid="uid-1", summary="Milk", status=TodoItemStatus.NEEDS_ACTION),
            TodoItem(uid="uid-2", summary="Milk", status=TodoItemStatus.NEEDS_ACTION),
        ]

    price_signal = "smart_shopping_route_synthetic-entry_price_update"

    def classifications(self) -> dict[str, str]:
        return {"uid:uid-1": "Dairy", "uid:uid-2": "Produce", "milk": "Dairy"}

    def section_for(self, item: TodoItem) -> str:
        return "Produce" if item.uid == "uid-2" else "Dairy"


def make_entity(*, prefix: bool = False):
    coordinator = Coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title="Example Supermarket",
        data={
            "profile_name": "Example Supermarket",
            "show_completed": False,
            "display_section_prefix": prefix,
            "ai_task_entity_id": "ai_task.synthetic",
        },
    )
    return SmartShoppingRouteTodo(entry, coordinator), coordinator


def test_duplicate_summaries_keep_uids_and_uid_specific_order() -> None:
    entity, _coordinator = make_entity()
    assert [(item.uid, item.summary) for item in entity.todo_items] == [
        ("uid-2", "Milk"),
        ("uid-1", "Milk"),
    ]


def test_update_delete_and_create_forward_exact_source_values() -> None:
    entity, coordinator = make_entity(prefix=True)
    assert [item.summary for item in entity.todo_items] == [
        "Produce · Milk",
        "Dairy · Milk",
    ]
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-2",
                summary="Produce · Oat milk",
                status=TodoItemStatus.COMPLETED,
            )
        )
    )
    asyncio.run(entity.async_delete_todo_items(["uid-2"]))
    asyncio.run(
        entity.async_create_todo_item(
            TodoItem(summary="Dairy · Cheese", status=TodoItemStatus.NEEDS_ACTION)
        )
    )
    assert coordinator.source.updated[0].uid == "uid-2"
    assert coordinator.source.updated[0].summary == "Oat milk"
    assert coordinator.source.deleted == [["uid-2"]]
    assert coordinator.source.created[0].uid is None
    assert coordinator.source.created[0].summary == "Cheese"


def test_prefix_is_never_stripped_when_the_feature_is_disabled() -> None:
    """With prefixes off the summary is user data, not integration decoration."""

    entity, coordinator = make_entity(prefix=False)
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-1",
                summary="Dairy · Milk",
                status=TodoItemStatus.NEEDS_ACTION,
            )
        )
    )
    assert coordinator.source.updated[0].summary == "Dairy · Milk"


def test_prefix_display_is_opt_in() -> None:
    entity, _coordinator = make_entity(prefix=False)
    assert [item.summary for item in entity.todo_items] == ["Milk", "Milk"]


def test_manual_move_is_not_proxied() -> None:
    entity, _coordinator = make_entity()
    features = TodoListEntityFeature(entity.supported_features)
    assert not features & TodoListEntityFeature.MOVE_TODO_ITEM
    assert features & TodoListEntityFeature.UPDATE_TODO_ITEM


def test_unknown_future_source_features_are_masked(monkeypatch) -> None:
    entity, coordinator = make_entity()
    coordinator.source.supported_features = int(
        TodoListEntityFeature.UPDATE_TODO_ITEM
    ) | (1 << 12)
    features = entity._proxy_features()
    # Unknown features and unsupported editable fields are not advertised.
    assert features & (1 << 12) == 0
    assert features & int(TodoListEntityFeature.UPDATE_TODO_ITEM)
    assert not features & int(TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)


def test_entity_is_grouped_under_one_profile_device() -> None:
    entity, _coordinator = make_entity()
    info = entity.device_info
    assert info["identifiers"] == {("smart_shopping_route", "synthetic-entry")}
    assert info["name"] == "Example Supermarket"
    assert info["entry_type"] == "service"


def test_entity_availability_follows_the_source_only(monkeypatch) -> None:
    entity, coordinator = make_entity()
    coordinator.available = True
    assert entity.available is True
    coordinator.available = False
    assert entity.available is False


def test_entity_uses_coordinator_sections(monkeypatch) -> None:
    entity, _coordinator = make_entity(prefix=True)
    seen = {}
    original = todo_module.strip_display_prefix

    def spy(summary, known_sections):
        seen["sections"] = tuple(known_sections)
        return original(summary, known_sections)

    monkeypatch.setattr(todo_module, "strip_display_prefix", spy)
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-1", summary="Dairy · Milk", status=TodoItemStatus.NEEDS_ACTION
            )
        )
    )
    assert seen["sections"] == ("Dairy",)


def test_price_is_rendered_with_size_and_unit_price() -> None:
    entity, coordinator = make_entity(prefix=True)
    coordinator.prices = FakePrices({
        "Milk": PriceQuote(
            chain="woolworths",
            product_id="123",
            product_name="Anchor Butter 500g",
            price=8.5,
            was_price=9.7,
            size="500g",
            unit_price="$1.70/100g",
        )
    })

    descriptions = [item.description for item in entity.todo_items]

    assert (
        "$8.50 · was $9.70 · 500g · $1.70/100g · Anchor Butter 500g" in descriptions[0]
    )
    # The generated block is marked, but carries no visible label: a prefix
    # repeated on every row wrapped the native list onto an extra line.
    assert descriptions[0].startswith("<!-- ssr-price:start sha256=")
    assert "\n$8.50 · was $9.70 · 500g" in descriptions[0]
    # Three lines, and a short digest: the block stays compact in the plain-text
    # editor where Home Assistant shows the comments.
    assert len(descriptions[0].splitlines()[:3]) == 3
    assert descriptions[0].splitlines()[2] == "<!-- ssr-price:end -->"
    assert "automatic" not in descriptions[0]
    assert "edit notes" not in descriptions[0]


def test_plain_price_has_no_was_suffix() -> None:
    entity, coordinator = make_entity()
    coordinator.prices = FakePrices({
        "Milk": PriceQuote(
            chain="paknsave", product_id="p1", product_name="Pams Milk 2L", price=4.0
        )
    })

    assert "$4.00 · Pams Milk 2L" in entity.todo_items[0].description


def test_unpriced_items_keep_no_description() -> None:
    entity, _coordinator = make_entity()

    assert all(item.description is None for item in entity.todo_items)


def test_price_lookup_uses_the_undecorated_summary() -> None:
    """With the prefix on, the summary is decorated but pricing keys off source."""

    entity, coordinator = make_entity(prefix=True)
    seen: list[str] = []

    class RecordingPrices(FakePrices):
        def quote_for(self, summary):
            seen.append(summary)
            return None

    coordinator.prices = RecordingPrices()

    items = entity.todo_items

    assert len(items) == 2
    assert seen == ["Milk", "Milk"]


def test_description_is_advertised_even_when_the_source_lacks_it() -> None:
    """Without it, Home Assistant rejects the whole save when renaming an item.

    The to-do dialog sends every field back, including the derived description,
    and validation fails the call if the entity does not advertise the feature.
    """

    entity, coordinator = make_entity()
    coordinator.source.supported_features = int(TodoListEntityFeature.UPDATE_TODO_ITEM)
    coordinator.prices.enabled = True
    entity._attr_supported_features = entity._proxy_features()

    assert entity.supported_features & int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )


def test_priced_item_shows_the_price_directly_above_notes() -> None:
    """The native list shows the price, not a repeated label, then the notes."""

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    coordinator.items[0].description = "Buy organic"
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    visible = [
        line
        for line in displayed.description.splitlines()
        if not line.startswith("<!-- ssr-price:")
    ]
    assert visible == ["$4.00 · Milk", "", "Buy organic"]


def test_a_searched_item_without_a_match_says_so() -> None:
    """Silence reads as a bug, so an unmatched item explains itself."""

    entity, coordinator = make_entity()
    coordinator.prices = FakePrices(failed={"milk"})
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    assert displayed.description.startswith("<!-- ssr-price:start sha256=")
    assert "no price match — refresh, rename or pin this item" in displayed.description


def test_an_item_that_has_not_been_searched_stays_quiet() -> None:
    """A newly added item is queued, not unmatched: no marker yet."""

    entity, coordinator = make_entity()
    coordinator.prices = FakePrices()
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    assert displayed.description is None


def test_updating_an_item_strips_only_our_price_block() -> None:
    entity, coordinator = make_entity(prefix=True)
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    displayed.summary = "Dairy · Oat milk"
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[0].summary == "Oat milk"
    assert coordinator.source.updated[0].description is None


@pytest.mark.parametrize("priced", [False, True])
def test_notes_survive_rename_completion_and_edit(priced) -> None:
    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    coordinator.items[0].description = "Keep chilled\nSecond line"
    if priced:
        coordinator.prices = FakePrices({
            "Milk": PriceQuote("paknsave", "1", "Milk", 4)
        })
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    assert "Keep chilled\nSecond line" in displayed.description
    displayed.summary = "Oat milk"
    displayed.status = TodoItemStatus.COMPLETED
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[-1].description == "Keep chilled\nSecond line"
    displayed.description = displayed.description.replace("Keep chilled", "Buy organic")
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[-1].description == "Buy organic\nSecond line"
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(uid="uid-1", summary="Milk", description="")
        )
    )
    assert coordinator.source.updated[-1].description == ""


def test_unsupported_notes_are_rejected_not_silently_lost() -> None:
    entity, coordinator = make_entity()
    for method in (entity.async_create_todo_item, entity.async_update_todo_item):
        with pytest.raises(ServiceValidationError, match="does not support notes"):
            asyncio.run(
                method(TodoItem(uid="uid-1", summary="Milk", description="Real note"))
            )
    assert not coordinator.source.created and not coordinator.source.updated


def test_create_preserves_supported_notes() -> None:
    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    asyncio.run(
        entity.async_create_todo_item(TodoItem(summary="Milk", description="Real note"))
    )
    assert coordinator.source.created[0].description == "Real note"


def test_a_price_block_edit_is_still_rejected() -> None:
    """The block is machine-checkable, so a hand edit is refused, not saved."""

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    displayed.description = displayed.description.replace("$4.00", "$0.01")
    with pytest.raises(ServiceValidationError, match="read-only"):
        asyncio.run(entity.async_update_todo_item(displayed))
    assert not coordinator.source.updated


def test_a_legacy_four_line_block_still_saves() -> None:
    """An item left open in a 0.8.7 dialog must not fail after the upgrade."""

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    price = "$4.00 · Milk"
    digest = sha256(price.encode()).hexdigest()
    legacy = (
        f"<!-- ssr-price:start -->\n{price}\n"
        f"<!-- ssr-price:sha256={digest} -->\n"
        "<!-- ssr-price:end -->\n\nKeep chilled"
    )
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-1",
                summary="Milk",
                description=legacy,
                status=TodoItemStatus.NEEDS_ACTION,
            )
        )
    )
    assert coordinator.source.updated[0].description == "Keep chilled"


def test_a_legacy_block_with_an_edited_price_is_rejected() -> None:
    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    legacy = (
        "<!-- ssr-price:start -->\n$0.01 · Milk\n"
        f"<!-- ssr-price:sha256={sha256('$4.00 · Milk'.encode()).hexdigest()} -->\n"
        "<!-- ssr-price:end -->"
    )
    with pytest.raises(ServiceValidationError, match="read-only"):
        asyncio.run(
            entity.async_update_todo_item(
                TodoItem(uid="uid-1", summary="Milk", description=legacy)
            )
        )
    assert not coordinator.source.updated


def test_a_priced_item_lists_better_value_alternatives() -> None:
    """The row answers "is the same thing cheaper elsewhere" as well as "what is it".

    The match is chosen for relevance, so the alternatives are what tells a
    shopper the pick was also the best value on that shelf.
    """

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    chosen = PriceQuote(
        "paknsave",
        "p1",
        "Centrum Advance Adults 100 Tablets",
        19.99,
        size="100pk",
        unit_price="$0.20/ea",
    )
    coordinator.prices = FakePrices(
        {"Milk": chosen},
        alternatives={
            "Milk": [
                chosen,
                PriceQuote(
                    "paknsave",
                    "p2",
                    "Centrum Advance Adult 100 Tablets",
                    24.99,
                    size="100pk",
                    unit_price="$0.25/ea",
                ),
            ]
        },
    )

    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    visible = [
        line
        for line in displayed.description.splitlines()
        if not line.startswith("<!-- ssr-price:")
    ]

    assert (
        visible[0] == "$19.99 · 100pk · $0.20/ea · Centrum Advance Adults 100 Tablets"
    )
    # The pick stays in the ranking so a shopper can see where it sits, but it is
    # not named twice: the line above already carries the product.
    assert visible[1] == (
        "Alternatives by unit price: "
        "$19.99 · 100pk · $0.20/ea (this item) | "
        "$24.99 · 100pk · $0.25/ea Centrum Advance Adult 100 Tablets"
    )


def test_the_chosen_product_is_shown_where_it_ranks_by_value() -> None:
    """A dearer pick must not look like the cheapest option on the shelf."""

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    chosen = PriceQuote(
        "paknsave",
        "p2",
        "Centrum Advance Adult 100 Tablets",
        24.99,
        size="100pk",
        unit_price="$0.25/ea",
    )
    coordinator.prices = FakePrices(
        {"Milk": chosen},
        alternatives={
            "Milk": [
                PriceQuote(
                    "paknsave",
                    "p1",
                    "Centrum Advance Adults 100 Tablets",
                    19.99,
                    size="100pk",
                    unit_price="$0.20/ea",
                ),
                chosen,
            ]
        },
    )

    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    visible = [
        line
        for line in displayed.description.splitlines()
        if not line.startswith("<!-- ssr-price:")
    ]

    assert visible[1] == (
        "Alternatives by unit price: "
        "$19.99 · 100pk · $0.20/ea Centrum Advance Adults 100 Tablets | "
        "$24.99 · 100pk · $0.25/ea (this item)"
    )


def test_a_single_option_gets_no_alternatives_line() -> None:
    """One product is not a comparison, and the line would be noise."""

    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    chosen = PriceQuote("paknsave", "p1", "Pams Milk 2L", 4.0, size="2L")
    coordinator.prices = FakePrices({"Milk": chosen}, alternatives={"Milk": [chosen]})

    displayed = next(i for i in entity.todo_items if i.uid == "uid-1")
    visible = [
        line
        for line in displayed.description.splitlines()
        if not line.startswith("<!-- ssr-price:")
    ]

    assert visible == ["$4.00 · 2L · Pams Milk 2L"]
