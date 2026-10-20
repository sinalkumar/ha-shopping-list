"""AI tie-break tests with no network or provider calls."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

ai_task_module = ModuleType("homeassistant.components.ai_task")
ai_task_module.async_generate_data = None
sys.modules.setdefault("homeassistant.components.ai_task", ai_task_module)

from smart_shopping_route import ai_match  # noqa: E402
from smart_shopping_route.prices import CHAIN_WOOLWORTHS, PriceQuote  # noqa: E402


def candidate(name: str, size: str | None = None) -> PriceQuote:
    return PriceQuote(
        chain=CHAIN_WOOLWORTHS,
        product_id=name,
        product_name=name,
        price=9.99,
        size=size,
    )


def test_the_ranker_returns_the_indexed_candidate(monkeypatch) -> None:
    """The answer is a choice among offered products, never a new one."""

    offered = [
        candidate("Centrum Advance Adult Multivitamin 100 Tablets", "100pk"),
        candidate("Centrum Advance Adult 100 Tablets", "100pk"),
    ]
    prompts: list[str] = []

    async def generate(*_args, **kwargs):
        prompts.append(kwargs["instructions"])
        return SimpleNamespace(data={"index": 1})

    monkeypatch.setattr(ai_match, "async_generate_data", generate)

    picked = asyncio.run(
        ai_match.AIMatchRanker(object(), "ai_task.synthetic").async_pick(
            "Centrum advance Adult 100pk", offered
        )
    )

    assert picked is offered[1]
    # Both products and their sizes must be in the prompt, or the model cannot
    # tell the variants apart.
    assert "Centrum Advance Adult 100 Tablets" in prompts[0]
    assert "Centrum Advance Adult Multivitamin 100 Tablets" in prompts[0]
    assert "100pk" in prompts[0]


def test_the_ranker_declines_with_minus_one(monkeypatch) -> None:
    """ "None of these" is a real answer and must not become a guess."""

    async def generate(*_args, **_kwargs):
        return SimpleNamespace(data={"index": -1})

    monkeypatch.setattr(ai_match, "async_generate_data", generate)

    picked = asyncio.run(
        ai_match.AIMatchRanker(object(), "ai_task.synthetic").async_pick(
            "Nappies", [candidate("Huggies Baby Wipes Fragrance Free 80pk")]
        )
    )

    assert picked is None


def test_the_ranker_makes_no_request_for_an_empty_shortlist(monkeypatch) -> None:
    """Every candidate was already ruled out, so there is nothing to ask."""

    async def generate(*_args, **_kwargs):
        raise AssertionError("no provider request expected")

    monkeypatch.setattr(ai_match, "async_generate_data", generate)

    picked = asyncio.run(
        ai_match.AIMatchRanker(object(), "ai_task.synthetic").async_pick(
            "Centrum advance Adult 100pk (not 50+)", []
        )
    )

    assert picked is None


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(data={"index": 5}),
        SimpleNamespace(data={"index": "not a number"}),
        SimpleNamespace(data="just text"),
        RuntimeError("provider exploded"),
    ],
)
def test_an_unusable_answer_leaves_the_item_unpriced(monkeypatch, result) -> None:
    """A bad answer must not attach a price nobody chose, and must not raise."""

    async def generate(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ai_match, "async_generate_data", generate)

    picked = asyncio.run(
        ai_match.AIMatchRanker(object(), "ai_task.synthetic").async_pick(
            "Nappies", [candidate("Huggies Ultra Dry Nappy Pants Walker Boy 32pk")]
        )
    )

    assert picked is None


def test_a_failed_tie_break_does_not_log_item_text(monkeypatch, caplog) -> None:
    """Item names stay out of logs here, exactly as in the price lookups."""

    async def generate(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_match, "async_generate_data", generate)

    with caplog.at_level("WARNING"):
        asyncio.run(
            ai_match.AIMatchRanker(object(), "ai_task.synthetic").async_pick(
                "Very Private Item Name",
                [candidate("Very Private Product Name")],
            )
        )

    assert "Very Private" not in caplog.text
