"""An in-memory provider for local development and tests.

It records what would have been sent, approves submitted templates immediately, and parses
webhook bodies in Meta's format, so a local app can be driven end to end with realistic payloads
(see ``greenline.testing``).
"""

from __future__ import annotations

import itertools
from typing import Any

from greenline.errors import ProviderError
from greenline.providers.meta_cloud import parse_webhook_payload
from greenline.schemas import Outbound, ProviderResult, Sender, WebhookEvent


class MockProvider:
    name = "mock"

    def __init__(self, *, auto_approve: bool = True) -> None:
        self.auto_approve = auto_approve
        self.sent: list[tuple[Sender, Outbound]] = []
        self.templates: dict[tuple[str, str, str], dict[str, str]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.failures: list[ProviderError] = []
        """Errors to raise from the next ``send`` calls, in order (for testing retries)."""
        self._ids = itertools.count(1)

    def send(self, sender: Sender, message: Outbound) -> ProviderResult:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((sender, message))
        return ProviderResult(ok=True, provider_message_id=f"wamid.mock.{next(self._ids):06d}", status="sent")

    def parse_webhook(self, payload: dict[str, Any]) -> list[WebhookEvent]:
        return parse_webhook_payload(payload)

    def create_template(
        self,
        waba_id: str,
        token: str,
        *,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
    ) -> dict[str, str]:
        template_id = f"tpl.mock.{next(self._ids):06d}"
        status = "approved" if self.auto_approve else "pending"
        self.submitted.append(
            {
                "waba_id": waba_id,
                "name": name,
                "language": language,
                "category": category,
                "components": components,
            }
        )
        self.templates[(waba_id, name, language)] = {
            "id": template_id,
            "name": name,
            "status": status,
            "category": category.lower(),
            "language": language,
            "reason": "",
            "body": next((str(c.get("text", "")) for c in components if c.get("type") == "BODY"), ""),
        }
        return {"id": template_id, "status": status}

    def list_templates(self, waba_id: str, token: str) -> list[dict[str, str]]:
        return [dict(t) for (w, _n, _l), t in self.templates.items() if w == waba_id]

    def set_template_status(self, waba_id: str, name: str, status: str, language: str = "en") -> None:
        """Change a stored template's review status (to simulate Meta's review)."""
        self.templates[(waba_id, name, language)]["status"] = status

    @property
    def last(self) -> Outbound:
        """The most recent outbound message."""
        return self.sent[-1][1]
