"""Helpers for tests and local simulators: webhook bodies in Meta's format, and signing.

body = testing.inbound_payload(phone_number_id="PN1", phone="15550100", text="hi")
raw, headers = testing.signed(body, app_secret)
client.post("/webhooks/whatsapp", content=raw, headers=headers)
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any

from greenline.security import sign
from greenline.webhook import SIGNATURE_HEADER

_ids = itertools.count(1)


def _envelope(waba_id: str, field: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba_id, "time": int(time.time()), "changes": [{"field": field, "value": value}]}],
    }


def _metadata(phone_number_id: str, display_phone: str) -> dict[str, str]:
    return {"display_phone_number": display_phone, "phone_number_id": phone_number_id}


def inbound_payload(
    *,
    phone_number_id: str,
    phone: str,
    text: str = "",
    waba_id: str = "WABA-TEST",
    message_id: str | None = None,
    button_payload: str = "",
    name: str = "Test User",
    display_phone: str = "15550100",
) -> dict[str, Any]:
    """An inbound text message, or a tapped quick-reply button when ``button_payload`` is given."""
    mid = message_id or f"wamid.in.{next(_ids):06d}"
    message: dict[str, Any] = {"from": phone, "id": mid, "timestamp": str(int(time.time()))}
    if button_payload:
        message.update(type="button", button={"text": text, "payload": button_payload})
    else:
        message.update(type="text", text={"body": text})
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(phone_number_id, display_phone),
        "contacts": [{"profile": {"name": name}, "wa_id": phone}],
        "messages": [message],
    }
    return _envelope(waba_id, "messages", value)


def status_payload(
    *,
    phone_number_id: str,
    provider_message_id: str,
    status: str,
    phone: str = "",
    waba_id: str = "WABA-TEST",
    error_code: str = "",
    display_phone: str = "15550100",
) -> dict[str, Any]:
    """A delivery receipt (``sent``, ``delivered``, ``read`` or ``failed``)."""
    st: dict[str, Any] = {
        "id": provider_message_id,
        "status": status,
        "timestamp": str(int(time.time())),
        "recipient_id": phone,
    }
    if error_code:
        st["errors"] = [{"code": int(error_code) if error_code.isdigit() else error_code, "title": "error"}]
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(phone_number_id, display_phone),
        "statuses": [st],
    }
    return _envelope(waba_id, "messages", value)


def template_status_payload(
    *, waba_id: str, name: str, status: str, language: str = "en", template_id: str = "", reason: str = ""
) -> dict[str, Any]:
    """A ``message_template_status_update`` notice (APPROVED, REJECTED, PAUSED...)."""
    value = {
        "event": status.upper(),
        "message_template_id": template_id,
        "message_template_name": name,
        "message_template_language": language,
        "reason": reason or "NONE",
    }
    return _envelope(waba_id, "message_template_status_update", value)


def quality_payload(
    *, waba_id: str, display_phone: str, event: str, current_limit: str = ""
) -> dict[str, Any]:
    """A ``phone_number_quality_update`` notice (FLAGGED, UNFLAGGED, UPGRADE, DOWNGRADE)."""
    value = {"display_phone_number": display_phone, "event": event.upper(), "current_limit": current_limit}
    return _envelope(waba_id, "phone_number_quality_update", value)


def signed(payload: dict[str, Any], app_secret: str) -> tuple[bytes, dict[str, str]]:
    """The raw body and headers of a POST signed the way Meta signs it."""
    raw = json.dumps(payload).encode()
    return raw, {SIGNATURE_HEADER: sign(app_secret, raw), "Content-Type": "application/json"}
