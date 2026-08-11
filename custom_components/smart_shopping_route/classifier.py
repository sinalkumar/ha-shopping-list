"""Provider-independent Home Assistant AI Task classification."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.ai_task import async_generate_data
from homeassistant.core import HomeAssistant

from .const import AI_TIMEOUT_SECONDS, FALLBACK_SECTION
from .models import RouteSection, section_names

_LOGGER = logging.getLogger(__name__)


class AITaskClassifier:
    """Classify one item into exactly one configured route section."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id

    async def async_classify(
        self, summary: str, sections: tuple[RouteSection, ...]
    ) -> tuple[str, float] | None:
        names = section_names(sections)
        structure = vol.Schema(
            {
                vol.Required("section"): vol.In(names),
                vol.Required("confidence"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
            },
            extra=vol.PREVENT_EXTRA,
        )
        route_text = "\n".join(
            f"- {section.name}: {section.hints or '(no hints provided)'}"
            for section in sections
        )
        instructions = (
            "You classify a shopping item into a configured supermarket route.\n"
            "Choose exactly one supplied section. The hints are authoritative. "
            "Do not invent sections or alter route order. If uncertain, choose "
            f"{FALLBACK_SECTION}.\n\nShopping item:\n{summary!r}\n\nSections:\n"
            f"{route_text}\n- {FALLBACK_SECTION}: unknown or low-confidence items"
        )
        try:
            async with asyncio.timeout(AI_TIMEOUT_SECONDS):
                result = await async_generate_data(
                    self.hass,
                    task_name="Classify shopping route item",
                    entity_id=self.entity_id,
                    instructions=instructions,
                    structure=structure,
                )
            data: Any = result.data
            if not isinstance(data, dict):
                raise ValueError("AI Task returned non-object data")
            validated = structure(data)
            return str(validated["section"]), float(validated["confidence"])
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("AI classification failed for 1 item")
            return None
