"""Tests for cache precedence, persistence, isolation, and bounds."""

from smart_shopping_route.cache import ClassificationCache


def test_cache_normalizes_and_manual_override_wins() -> None:
    cache = ClassificationCache()
    cache.set("  MILK ", "Dairy")
    assert cache.get("milk") == "Dairy"
    cache.set("milk", "Household", manual=True)
    cache.set("Milk", "Dairy")
    assert cache.get("MILK") == "Household"


def test_profiles_are_independent_and_uid_override_is_specific() -> None:
    first = ClassificationCache()
    second = ClassificationCache()
    first.set("Milk", "Dairy")
    second.set("Milk", "Pantry")
    first.set_uid("uid-2", "Produce")
    assert first.get("Milk", "uid-1") == "Dairy"
    assert first.get("Milk", "uid-2") == "Produce"
    assert second.get("Milk", "uid-2") == "Pantry"


def test_round_trip_reclassify_and_section_invalidation() -> None:
    cache = ClassificationCache()
    cache.set("Milk", "Dairy")
    cache.set("Bread", "Bakery", manual=True)
    restored = ClassificationCache.from_dict(cache.as_dict())
    restored.clear_ai()
    assert restored.get("Milk") is None
    assert restored.get("Bread") == "Bakery"
    restored.invalidate_sections({"Produce"})
    assert restored.get("Bread") is None


def test_lru_cap_preserves_manual_entries() -> None:
    cache = ClassificationCache(max_entries=2)
    cache.set("Bread", "Bakery", manual=True)
    cache.set("Milk", "Dairy")
    cache.set("Apples", "Produce")
    assert len(cache.entries) == 2
    assert cache.get("Bread") == "Bakery"
