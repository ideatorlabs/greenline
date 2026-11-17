"""Per-message cost estimates.

Meta bills per delivered message, at a rate that depends on the message category and the
recipient's market. A ``RateCard`` holds those rates in the currency's minor unit (cents,
paise...). The engine prices each outbound message when it is sent and stores the estimate on the
message row, so you can meter usage per tenant and reconcile against Meta's invoice later.

Greenline does not ship authoritative prices. ``RateCard.example_inr()`` loads an *example* card for
India from package data; check Meta's current rate card for your markets before relying on it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

EXAMPLE_INR_RESOURCE = "rate_card.example-inr.json"


@dataclass(frozen=True)
class RateCard:
    currency: str
    rates: Mapping[str, int]
    """Category -> price per message in minor units."""
    minor_units: int = 100
    """Minor units per major unit (100 paise per rupee)."""
    free_in_window: frozenset[str] = field(default_factory=frozenset)
    """Categories that cost nothing while the customer-service window is open."""
    free_service_per_number_per_month: int = 0
    """Free-form (service) replies each number may send per calendar month before they are billed."""
    note: str = ""

    def cost(self, category: str, *, in_window: bool = False, service_sent_this_month: int = 0) -> int:
        """Estimated price of one message, in minor units."""
        if in_window and category in self.free_in_window:
            return 0
        if category == "service" and service_sent_this_month < self.free_service_per_number_per_month:
            return 0
        return int(self.rates.get(category, self.rates.get("utility", 0)))

    def to_major(self, minor: int) -> float:
        return round(minor / self.minor_units, 2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RateCard:
        return cls(
            currency=str(data["currency"]),
            rates={str(k): int(v) for k, v in dict(data["rates"]).items()},
            minor_units=int(data.get("minor_units", 100)),
            free_in_window=frozenset(data.get("free_in_window", ())),
            free_service_per_number_per_month=int(data.get("free_service_per_number_per_month", 0)),
            note=str(data.get("note", "")),
        )

    @classmethod
    def from_json(cls, text: str) -> RateCard:
        return cls.from_dict(json.loads(text))

    @classmethod
    def example_inr(cls) -> RateCard:
        """An example India rate card. Example only: verify current Meta pricing."""
        data_dir = resources.files("greenline").joinpath("data")
        text = data_dir.joinpath(EXAMPLE_INR_RESOURCE).read_text("utf-8")
        return cls.from_json(text)


FREE = RateCard(currency="XXX", rates={}, note="no metering")


def budget_status(spent_minor: int, budget_minor: int, warn_at: float = 0.8) -> dict[str, Any]:
    """Where spend stands against a budget: ``{spent, budget, share, warn, exhausted}``."""
    share = spent_minor / budget_minor if budget_minor > 0 else 0.0
    return {
        "spent": spent_minor,
        "budget": budget_minor,
        "share": round(share, 3),
        "warn": budget_minor > 0 and share >= warn_at,
        "exhausted": budget_minor > 0 and share >= 1,
    }
