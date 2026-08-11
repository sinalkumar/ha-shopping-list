"""AI Task classifier tests with no network or provider calls."""

import asyncio
import logging
import sys
from enum import IntFlag
from types import ModuleType, SimpleNamespace


class AITaskEntityFeature(IntFlag):
    GENERATE_DATA = 1


ai_task_module = ModuleType("homeassistant.components.ai_task")
ai_task_module.AITaskEntityFeature = AITaskEntityFeature
ai_task_module.async_generate_data = None
sys.modules["homeassistant.components.ai_task"] = ai_task_module

from smart_shopping_route import classifier  # noqa: E402
from smart_shopping_route.models import RouteSection  # noqa: E402

SECTIONS = (RouteSection("Produce", "fruit"), RouteSection("Dairy", "milk"))


def test_structured_result_is_accepted(monkeypatch) -> None:
    async def generate(*_args, **kwargs):
        assert "Milk" in kwargs["instructions"]
        return SimpleNamespace(data={"section": "Dairy", "confidence": 0.9})

    monkeypatch.setattr(classifier, "async_generate_data", generate)
    result = asyncio.run(
        classifier.AITaskClassifier(object(), "ai_task.synthetic").async_classify(
            "Milk", SECTIONS
        )
    )
    assert result == ("Dairy", 0.9)


def test_unknown_or_malformed_result_falls_back_without_item_log(
    monkeypatch, caplog
) -> None:
    async def generate(*_args, **_kwargs):
        return SimpleNamespace(data={"section": "Invented", "confidence": 0.5})

    monkeypatch.setattr(classifier, "async_generate_data", generate)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            classifier.AITaskClassifier(object(), "ai_task.synthetic").async_classify(
                "Synthetic private item", SECTIONS
            )
        )
    assert result is None
    assert "Synthetic private item" not in caplog.text
    assert "failed for 1 item" in caplog.text


def test_provider_exception_is_isolated(monkeypatch) -> None:
    async def generate(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(classifier, "async_generate_data", generate)
    assert (
        asyncio.run(
            classifier.AITaskClassifier(object(), "ai_task.synthetic").async_classify(
                "Bread", SECTIONS
            )
        )
        is None
    )
