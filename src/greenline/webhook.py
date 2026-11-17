"""Framework-neutral webhook handling.

``engine.webhook.verify(query_params)`` answers Meta's GET handshake and
``engine.webhook.receive(raw_body, signature_header)`` handles a POST. Both return
``(http_status, body)`` so any web framework can wrap them in a few lines; the FastAPI wrapper is
in ``greenline.ext.fastapi``.

A POST with a missing or wrong ``X-Hub-Signature-256`` gets 403. Everything else gets 200, even a
body that cannot be parsed or an event that fails: Meta retries (and eventually disables) webhooks
that return errors, and a retry would not fix a bad payload. Failures are logged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from greenline.security import verify_signature

if TYPE_CHECKING:
    from greenline.engine import Engine

log = logging.getLogger("greenline.webhook")

SIGNATURE_HEADER = "X-Hub-Signature-256"


class WebhookHandler:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def verify(self, params: Mapping[str, str]) -> tuple[int, str]:
        """The GET handshake: echo ``hub.challenge`` when the verify token matches."""
        expected = self.engine.settings.verify_token
        if expected and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
            return 200, params.get("hub.challenge", "")
        return 403, "forbidden"

    def receive(self, raw_body: bytes, signature: str | None) -> tuple[int, dict[str, Any]]:
        """Verify the signature on the raw body, then process every event in it."""
        if not verify_signature(self.engine.settings.app_secret, raw_body, signature):
            return 403, {"error": "invalid signature"}
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            log.warning("webhook body is not JSON; acknowledged and dropped")
            return 200, {"processed": 0}
        if not isinstance(payload, dict):
            return 200, {"processed": 0}
        try:
            events = self.engine.provider.parse_webhook(payload)
        except Exception:
            log.exception("could not parse webhook payload")
            return 200, {"processed": 0}
        processed = 0
        for event in events:
            try:  # one bad event must not drop the rest
                processed += int(self.engine.process_event(event))
            except Exception:
                log.exception("processing %s event %s failed", event.kind, event.event_id)
        return 200, {"processed": processed}
