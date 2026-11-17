"""A tiny synchronous event bus.

The engine emits events only after the database transaction that produced them has committed,
so a subscriber can read the rows it is told about. A failing subscriber is logged and skipped;
it never breaks a send or a webhook.

Events and their keyword payloads:

``inbound``
    tenant_id, phone, text, message_type, button_payload, provider_message_id, message_id,
    phone_number_id, external_id
``status``
    tenant_id, phone, status, error_code, provider_message_id, message_id
``opt_out`` / ``opt_in``
    tenant_id, phone, phone_number_id, external_id
``template_status``
    waba_id, name, language, status, reason
``template_category``
    waba_id, name, language, category
``quality``
    waba_id, display_phone, quality, tier

``tenant_id`` is ``""`` when an inbound message on a shared number cannot be attributed, or a
receipt is for a message this engine did not send. Subscribers should accept ``**kwargs`` so new
payload keys do not break them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

log = logging.getLogger("greenline.hooks")

Handler = Callable[..., Any]

EVENTS = ("inbound", "status", "opt_out", "opt_in", "template_status", "template_category", "quality")


class Hooks:
    """Per-engine subscriber registry."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: str, handler: Handler | None = None) -> Any:
        """Subscribe ``handler`` to ``event``. Usable as a decorator: ``@hooks.on("inbound")``.

        Subscribing the same callable twice to one event is a no-op.
        """

        def register(fn: Handler) -> Handler:
            if fn not in self._subscribers[event]:
                self._subscribers[event].append(fn)
            return fn

        if handler is None:
            return register
        return register(handler)

    def off(self, event: str, handler: Handler) -> None:
        if handler in self._subscribers.get(event, []):
            self._subscribers[event].remove(handler)

    def emit(self, event: str, /, **payload: Any) -> None:
        for fn in list(self._subscribers.get(event, [])):
            try:
                fn(**payload)
            except Exception:
                log.exception("subscriber %r for %r failed", getattr(fn, "__name__", fn), event)

    def clear(self) -> None:
        self._subscribers.clear()

    def subscribers(self, event: str) -> list[Handler]:
        return list(self._subscribers.get(event, []))
