"""The provider port. Every backend (Meta Cloud API, a BSP, the mock) implements this."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from greenline.schemas import Outbound, ProviderResult, Sender, WebhookEvent


@runtime_checkable
class Provider(Protocol):
    """What the engine needs from a messaging backend.

    ``send`` returns a :class:`ProviderResult` on success and raises
    :class:`~greenline.errors.ProviderError` (or ``RateLimited``) on failure, with
    ``retryable`` set when trying again could help.
    """

    name: str

    def send(self, sender: Sender, message: Outbound) -> ProviderResult: ...

    def parse_webhook(self, payload: dict[str, Any]) -> list[WebhookEvent]: ...

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
        """Submit a template for review. Returns ``{"id": ..., "status": ...}`` (status lowercase)."""
        ...

    def list_templates(self, waba_id: str, token: str) -> list[dict[str, str]]:
        """Every template on the account: ``[{"id", "name", "status", "category", "language", "reason"}]``."""
        ...
