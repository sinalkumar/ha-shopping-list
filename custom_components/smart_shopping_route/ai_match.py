"""Optional AI tie-break for price matches that scoring cannot separate.

Deterministic matching handles the overwhelming majority of items. What it
cannot do is read intent: "Centrum advance Adult 100pk (not 50+)" and the two
Centrum Advance Adult 100 products in the shop are a genuine ambiguity that no
token arithmetic resolves, because both candidates are the same words.

So this module is a last resort, and it is deliberately narrow:

* It only ever sees candidates that already passed the hard constraints in
  prices.shortlist(): exact pack size, and any quantity the item rules out. It
  therefore cannot override those constraints, only choose among what survived.
* It may only answer with an index into that list, or -1 for "none of these".
  It cannot invent a product, a price, or a variant.
* It is opt-in. Item and product names leave the house for the configured
  provider, which is a real change from the otherwise anonymous lookups, so it
  is off unless the profile turns it on.
* Its answer is cached by the caller as an automatic pin. Re-asking every cycle
  would let the choice flip between refreshes and corrupt the price history the
  drop detection is built on.

Privacy: item and product text are never logged here. A failure logs a count.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.ai_task import async_generate_data
from homeassistant.core import HomeAssistant

from .const import AI_TIMEOUT_SECONDS
from .prices import PriceQuote

_LOGGER = logging.getLogger(__name__)


class AIMatchRanker:
    """Ask the profile's AI Task which listed product an item means."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id

    async def async_pick(
        self, summary: str, candidates: list[PriceQuote]
    ) -> PriceQuote | None:
        """Return one of ``candidates``, or None when nothing fits.

        ``candidates`` is expected to be the deterministic shortlist. An empty
        list means every candidate was already ruled out, so there is nothing to
        ask about and no request is made.
        """

        if not candidates:
            return None

        structure = vol.Schema(
            {
                vol.Required("index"): vol.All(
                    vol.Coerce(int), vol.Range(min=-1, max=len(candidates) - 1)
                ),
            },
            extra=vol.PREVENT_EXTRA,
        )
        listing = "\n".join(
            f"{index}. {candidate.product_name}"
            + (f" [{candidate.size}]" if candidate.size else "")
            for index, candidate in enumerate(candidates)
        )
        instructions = (
            "You choose which supermarket product a shopping list item means.\n"
            "The item may name a brand, a variant, an age formulation or a pack "
            "size, and the listed products may differ in exactly those ways. "
            "Choose the product that is the same thing the item asks for, not a "
            "different size, a different variant, or a different age group.\n"
            "Answer with the number of the single best product. If none of them "
            "is the same thing, answer -1. Never invent a product.\n\n"
            f"Shopping item:\n{summary!r}\n\nProducts:\n{listing}"
        )
        try:
            async with asyncio.timeout(AI_TIMEOUT_SECONDS):
                result = await async_generate_data(
                    self.hass,
                    task_name="Choose the matching supermarket product",
                    entity_id=self.entity_id,
                    instructions=instructions,
                    structure=structure,
                )
            data: Any = result.data
            if not isinstance(data, dict):
                raise ValueError("AI Task returned non-object data")
            index = int(structure(data)["index"])
        except asyncio.CancelledError:
            raise
        except Exception:
            # The provider, the schema or the timeout failed. Falling back to
            # "no match" keeps the item unpriced and retryable rather than
            # attaching a price nobody chose.
            _LOGGER.warning("AI product tie-break failed for 1 item")
            return None
        if index < 0:
            return None
        return candidates[index]
