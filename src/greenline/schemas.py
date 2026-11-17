"""Plain data objects passed between the engine, providers and callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Category = Literal["marketing", "utility", "authentication", "service"]
"""Meta's conversation categories. ``service`` is a free-form reply inside the customer-service window."""

EventKind = Literal["inbound", "status", "template_status", "template_category", "quality", "unknown"]

CATEGORIES: tuple[str, ...] = ("marketing", "utility", "authentication", "service")


@dataclass
class Sender:
    """Everything a provider needs to send from one phone number."""

    phone_number_id: str
    token: str
    waba_id: str = ""
    display_phone: str = ""
    display_name: str = ""


@dataclass
class TemplateMessage:
    """A template send.

    ``params`` fill the body variables in order. When ``param_count`` is set, the params are
    sanitised and padded or truncated to exactly that many before sending.

    ``buttons`` holds one entry per dynamic button, in template order, for example
    ``{"sub_type": "url", "index": 0, "text": "order/42"}`` or
    ``{"sub_type": "quick_reply", "index": 1, "payload": "stop"}``.
    """

    name: str
    language: str = "en"
    category: Category = "utility"
    params: list[str] = field(default_factory=list)
    param_count: int | None = None
    header_media_url: str = ""
    header_media_id: str = ""
    header_document_name: str = ""
    buttons: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Outbound:
    """One outbound message as handed to a provider."""

    kind: Literal["text", "template"]
    to: str
    category: Category = "utility"
    text: str = ""
    template: TemplateMessage | None = None
    preview_url: bool = True


@dataclass
class ProviderResult:
    ok: bool
    provider_message_id: str = ""
    status: str = "queued"
    error_code: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    """One normalised webhook item: an inbound message, a delivery receipt, or an account notice."""

    kind: EventKind
    event_id: str
    phone_number_id: str = ""
    waba_id: str = ""
    phone: str = ""
    """Inbound: the sender. Status: the recipient. Quality: the number's display phone."""
    text: str = ""
    message_type: str = ""
    button_payload: str = ""
    provider_message_id: str = ""
    status: str = ""
    error_code: str = ""
    template_name: str = ""
    template_id: str = ""
    template_language: str = ""
    template_status: str = ""
    category: str = ""
    reason: str = ""
    quality: str = ""
    tier: str = ""
    timestamp: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """Outcome of a send. ``skipped`` names the reason when the engine refused to send."""

    ok: bool
    message_id: str = ""
    provider_message_id: str = ""
    status: str = ""
    kind: str = ""
    template_name: str = ""
    skipped: str = ""
    error: str = ""
    error_code: str = ""
