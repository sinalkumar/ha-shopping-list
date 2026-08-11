"""Static privacy guardrails for public diagnostics and logs."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_diagnostics_do_not_serialize_items_or_entry_data() -> None:
    source = (
        ROOT / "custom_components/smart_shopping_route/diagnostics.py"
    ).read_text()
    assert '"items"' not in source
    assert '"summary"' not in source
    assert '"uid"' not in source
    assert "entry.data" in source  # dependency lookup only; no returned identifiers


def test_error_logs_do_not_format_item_summary() -> None:
    source = (ROOT / "custom_components/smart_shopping_route/classifier.py").read_text()
    assert '_LOGGER.warning("AI classification failed for 1 item"' in source
