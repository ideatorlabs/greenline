"""Meta's WhatsApp Business Cloud API, called directly over the Graph API with httpx.

The base URL and API version are configurable, and an ``httpx.Client`` can be injected, so tests
run against ``httpx.MockTransport`` and never reach Meta.
"""

from __future__ import annotations

from typing import Any

import httpx

from greenline.config import Settings
from greenline.errors import ProviderError, RateLimited
from greenline.schemas import Outbound, ProviderResult, Sender, TemplateMessage, WebhookEvent

# Meta error codes worth another attempt (throttling, temporary service errors).
RETRYABLE_CODES = frozenset({"130429", "131056", "133016", "80007", "1", "2", "4"})
# Codes that will not succeed on retry: bad or blocked recipient, closed window, template problems.
PERMANENT_CODES = frozenset({"131047", "131026", "131051", "132000", "132001", "132005", "132007", "131009"})
RATE_LIMIT_CODES = frozenset({"130429", "131056"})
TOKEN_EXPIRED_CODE = "190"

_MAX_TEMPLATE_PAGES = 50


class MetaCloudProvider:
    name = "meta_cloud"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        api_version: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        s = settings or Settings(provider="meta_cloud")
        self.base_url = (base_url or s.graph_base_url).rstrip("/")
        self.api_version = api_version or s.graph_version
        self.timeout = timeout if timeout is not None else s.http_timeout
        self._client = client or httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # -- messages ---------------------------------------------------------------------------

    def send(self, sender: Sender, message: Outbound) -> ProviderResult:
        body = build_message_body(message)
        try:
            response = self._client.post(
                self.url(f"{sender.phone_number_id}/messages"),
                headers=self._headers(sender.token),
                json=body,
                timeout=self.timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached Meta, so trying again is safe.
            raise ProviderError(f"network: {exc}", code="network", retryable=True) from exc
        except httpx.RequestError as exc:
            # The request may have reached Meta (read timeout, dropped connection). Sending again
            # could deliver and bill the message twice, so this is not retried automatically.
            raise ProviderError(
                f"network, delivery unknown: {exc}", code="network_unknown", retryable=False
            ) from exc
        return interpret_send_response(response)

    # -- templates --------------------------------------------------------------------------

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
        payload = {
            "name": name,
            "language": language,
            "category": (category or "utility").upper(),
            "components": components,
        }
        try:
            response = self._client.post(
                self.url(f"{waba_id}/message_templates"), headers=self._headers(token), json=payload
            )
        except httpx.RequestError as exc:
            raise ProviderError(f"network: {exc}", code="network", retryable=True) from exc
        data = _json(response)
        if response.status_code >= 300:
            raise ProviderError(
                error_text(response), code="template_create_failed", http_status=response.status_code
            )
        return {"id": str(data.get("id", "")), "status": str(data.get("status", "PENDING")).lower()}

    def list_templates(self, waba_id: str, token: str) -> list[dict[str, str]]:
        url = self.url(f"{waba_id}/message_templates")
        params: dict[str, Any] | None = {
            "fields": "id,name,status,category,language,rejected_reason,components",
            "limit": 200,
        }
        out: list[dict[str, str]] = []
        for _ in range(_MAX_TEMPLATE_PAGES):
            try:
                response = self._client.get(url, headers=self._headers(token), params=params)
            except httpx.RequestError as exc:
                raise ProviderError(f"network: {exc}", code="network", retryable=True) from exc
            if response.status_code >= 300:
                raise ProviderError(
                    error_text(response), code="template_list_failed", http_status=response.status_code
                )
            data = _json(response)
            for item in data.get("data", []):
                reason = item.get("rejected_reason")
                out.append(
                    {
                        "id": str(item.get("id", "")),
                        "name": str(item.get("name", "")),
                        "status": str(item.get("status", "")).lower(),
                        "category": str(item.get("category", "")).lower(),
                        "language": str(item.get("language", "")),
                        "reason": "" if reason in (None, "", "NONE") else str(reason),
                        "body": _body_text(item.get("components")),
                    }
                )
            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break
            url, params = next_url, None  # the cursor URL already carries the query
        return out

    # -- webhooks ---------------------------------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any]) -> list[WebhookEvent]:
        return parse_webhook_payload(payload)


# -- request building -----------------------------------------------------------------------


def build_message_body(message: Outbound) -> dict[str, Any]:
    """The JSON body for ``POST /{phone_number_id}/messages``."""
    body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": message.to}
    if message.kind == "text":
        body["type"] = "text"
        body["text"] = {"preview_url": message.preview_url, "body": message.text}
        return body
    if message.template is None:
        raise ValueError("a template message needs a template")
    body["type"] = "template"
    body["template"] = build_template_object(message.template)
    return body


def build_template_object(t: TemplateMessage) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if t.header_media_url or t.header_media_id:
        media: dict[str, Any] = (
            {"link": t.header_media_url} if t.header_media_url else {"id": t.header_media_id}
        )
        if t.header_document_name:
            media["filename"] = t.header_document_name
            components.append({"type": "header", "parameters": [{"type": "document", "document": media}]})
        else:
            components.append({"type": "header", "parameters": [{"type": "image", "image": media}]})
    if t.params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in t.params]})
    components.extend(button_component(b) for b in t.buttons)
    out: dict[str, Any] = {"name": t.name, "language": {"code": t.language}}
    if components:
        out["components"] = components
    return out


def button_component(button: dict[str, Any]) -> dict[str, Any]:
    """One send-time button parameter: a URL suffix, a quick-reply payload or a coupon code."""
    sub_type = button.get("sub_type", "url")
    if sub_type == "quick_reply":
        param = {"type": "payload", "payload": str(button.get("payload", ""))}
    elif sub_type == "copy_code":
        param = {"type": "coupon_code", "coupon_code": str(button.get("coupon_code", ""))}
    else:
        param = {"type": "text", "text": str(button.get("text", ""))}
    return {
        "type": "button",
        "sub_type": sub_type,
        "index": str(button.get("index", 0)),
        "parameters": [param],
    }


# -- response handling ----------------------------------------------------------------------


def interpret_send_response(response: httpx.Response) -> ProviderResult:
    """Turn a send response into a result, or raise a typed error."""
    data = _json(response)
    if response.status_code < 300:
        messages = data.get("messages") or [{}]
        return ProviderResult(
            ok=True, provider_message_id=str(messages[0].get("id", "")), status="sent", raw=data
        )
    err = data.get("error") or {}
    code = str(err.get("code", response.status_code))
    detail = str((err.get("error_data") or {}).get("details", "") or err.get("message", ""))
    if response.status_code == 429 or code in RATE_LIMIT_CODES:
        retry_after = response.headers.get("Retry-After")
        raise RateLimited(
            detail or "rate limited",
            retry_after=float(retry_after) if retry_after else None,
            http_status=response.status_code,
        )
    retryable = code not in PERMANENT_CODES and (code in RETRYABLE_CODES or response.status_code >= 500)
    raise ProviderError(
        detail or f"HTTP {response.status_code}",
        code=code,
        retryable=retryable,
        http_status=response.status_code,
    )


def _body_text(components: Any) -> str:
    for c in components or []:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY":
            return str(c.get("text", ""))
    return ""


def error_text(response: httpx.Response) -> str:
    data = _json(response)
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("error_user_msg") or err.get("message") or ""
        if msg:
            return f"{msg} (code {err.get('code', response.status_code)})"
    return f"HTTP {response.status_code}"


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# -- webhook parsing ------------------------------------------------------------------------


def parse_webhook_payload(payload: dict[str, Any]) -> list[WebhookEvent]:
    """Normalise a Cloud API webhook body into events.

    Handles ``messages`` (text, quick-reply buttons, interactive replies; other types arrive with
    empty text and their ``message_type``), ``statuses``, ``message_template_status_update``,
    ``template_category_update`` and ``phone_number_quality_update``.
    """
    events: list[WebhookEvent] = []
    for entry in payload.get("entry", []) or []:
        waba_id = str(entry.get("id", ""))
        entry_time = str(entry.get("time", ""))
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            field = change.get("field", "")
            if field == "message_template_status_update":
                events.append(_template_status_event(waba_id, entry_time, value))
                continue
            if field == "template_category_update":
                events.append(_template_category_event(waba_id, entry_time, value))
                continue
            if field == "phone_number_quality_update":
                events.append(_quality_event(waba_id, entry_time, value))
                continue
            number_id = str((value.get("metadata") or {}).get("phone_number_id", ""))
            for m in value.get("messages", []) or []:
                events.append(_inbound_event(waba_id, number_id, m))
            for st in value.get("statuses", []) or []:
                errors = st.get("errors") or [{}]
                events.append(
                    WebhookEvent(
                        kind="status",
                        event_id=f"st:{st.get('id', '')}:{st.get('status', '')}",
                        phone_number_id=number_id,
                        waba_id=waba_id,
                        phone=str(st.get("recipient_id", "")),
                        provider_message_id=str(st.get("id", "")),
                        status=str(st.get("status", "")).lower(),
                        error_code=str(errors[0].get("code", "")),
                        timestamp=_int(st.get("timestamp")),
                        raw=st,
                    )
                )
    return events


def _inbound_event(waba_id: str, number_id: str, m: dict[str, Any]) -> WebhookEvent:
    mtype = str(m.get("type", ""))
    text, payload = "", ""
    if mtype == "text":
        text = str((m.get("text") or {}).get("body", ""))
    elif mtype == "button":
        button = m.get("button") or {}
        text, payload = str(button.get("text", "")), str(button.get("payload", ""))
    elif mtype == "interactive":
        interactive = m.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        text, payload = str(reply.get("title", "")), str(reply.get("id", ""))
    elif mtype in ("image", "video", "document"):
        text = str((m.get(mtype) or {}).get("caption", ""))
    return WebhookEvent(
        kind="inbound",
        event_id=f"in:{m.get('id', '')}",
        phone_number_id=number_id,
        waba_id=waba_id,
        phone=str(m.get("from", "")),
        text=text,
        message_type=mtype,
        button_payload=payload,
        provider_message_id=str(m.get("id", "")),
        timestamp=_int(m.get("timestamp")),
        raw=m,
    )


def _template_status_event(waba_id: str, entry_time: str, v: dict[str, Any]) -> WebhookEvent:
    template_id = str(v.get("message_template_id", "") or "")
    status = str(v.get("event", "") or "").lower()
    name = str(v.get("message_template_name", "") or "")
    return WebhookEvent(
        kind="template_status",
        event_id=f"tpl:{waba_id}:{template_id or name}:{status}:{entry_time}",
        waba_id=waba_id,
        template_id=template_id,
        template_name=name,
        template_language=str(v.get("message_template_language", "") or ""),
        template_status=status,
        reason=str(v.get("reason", "") or ""),
        timestamp=_int(entry_time),
        raw=v,
    )


def _template_category_event(waba_id: str, entry_time: str, v: dict[str, Any]) -> WebhookEvent:
    template_id = str(v.get("message_template_id", "") or "")
    new = str(v.get("new_category") or v.get("correct_category") or "").lower()
    previous = str(v.get("previous_category") or "").lower()
    return WebhookEvent(
        kind="template_category",
        event_id=f"cat:{waba_id}:{template_id}:{new}:{entry_time}",
        waba_id=waba_id,
        template_id=template_id,
        template_name=str(v.get("message_template_name", "") or ""),
        template_language=str(v.get("message_template_language", "") or ""),
        category=new,
        reason=f"{previous} -> {new}",
        timestamp=_int(entry_time),
        raw=v,
    )


def _quality_event(waba_id: str, entry_time: str, v: dict[str, Any]) -> WebhookEvent:
    phone = str(v.get("display_phone_number", "") or "")
    event = str(v.get("event", "") or "").lower()
    return WebhookEvent(
        kind="quality",
        event_id=f"q:{waba_id}:{phone}:{event}:{entry_time}",
        waba_id=waba_id,
        phone=phone,
        quality=event,
        tier=str(v.get("current_limit", "") or ""),
        timestamp=_int(entry_time),
        raw=v,
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
