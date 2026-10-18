"""Store choices bind current and historical prices to the same public store."""

import asyncio

import pytest

from test_config_validation import STORES, _with_stores, make_flow, profile_input


@pytest.mark.parametrize(
    ("chain", "history", "error"),
    [
        ("paknsave", "2", "grocer_store_chain_mismatch"),
        ("woolworths", "228", "grocer_store_chain_mismatch"),
        ("paknsave", "217", "grocer_store_mismatch"),
        ("paknsave", "999999", "grocer_store_not_found"),
        ("paknsave", "-1", "grocer_store_invalid"),
        ("paknsave", "0", "grocer_store_invalid"),
        ("paknsave", "not a store", "grocer_store_invalid"),
    ],
)
def test_invalid_history_stores_are_refused(monkeypatch, chain, history, error):
    flow = make_flow(monkeypatch)
    _with_stores(monkeypatch, STORES)
    result = asyncio.run(
        flow.async_step_user(
            profile_input(
                price_chain=chain, price_store_id="Ormiston", grocer_store_id=history
            )
        )
    )
    assert result["errors"] == {"grocer_store_id": error}


def test_matching_store_names_are_resolved_and_saved(monkeypatch):
    flow = make_flow(monkeypatch)
    _with_stores(monkeypatch, STORES)
    asyncio.run(
        flow.async_step_user(
            profile_input(
                price_chain="paknsave", price_store_id="Ormiston", grocer_store_id="228"
            )
        )
    )
    assert flow.created["data"]["grocer_store_id"] == 228
    assert flow.created["data"]["price_store_name"] == "PAK'nSAVE Ormiston"
    assert flow.created["data"]["grocer_store_name"] == "PAK'nSAVE Ormiston"


def test_woolworths_online_scope_is_explicit(monkeypatch):
    flow = make_flow(monkeypatch)
    asyncio.run(
        flow.async_step_user(
            profile_input(price_chain="woolworths", grocer_store_id="2")
        )
    )
    assert "not store-specific" in flow.created["data"]["price_store_name"]
