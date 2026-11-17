"""The Meta Cloud API adapter, against httpx.MockTransport. Never calls Meta."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from greenline import (
    Engine,
    MetaCloudProvider,
    Outbound,
    ProviderError,
    RateLimited,
    Sender,
    Settings,
    Storage,
    TemplateDefinition,
    TemplateMessage,
)
from greenline.providers.meta_cloud import build_message_body, parse_webhook_payload

BASE = "https://graph.test.invalid"
SENDER = Sender(phone_number_id="PN1", token="tok-1", waba_id="W1")


def make(
    handler: Callable[[httpx.Request], httpx.Response], **kw: Any
) -> tuple[MetaCloudProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    settings = Settings(provider="meta_cloud", graph_base_url=BASE, graph_version="v99.0")
    return MetaCloudProvider(settings, client=client, **kw), seen


def ok_send(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]})


def test_text_send_request_shape() -> None:
    p, seen = make(ok_send)
    res = p.send(SENDER, Outbound(kind="text", to="15550101", category="service", text="Hello"))
    assert res.ok and res.provider_message_id == "wamid.ABC" and res.status == "sent"
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/v99.0/PN1/messages"
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550101",
        "type": "text",
        "text": {"preview_url": True, "body": "Hello"},
    }


def test_template_send_request_shape() -> None:
    p, seen = make(ok_send)
    tm = TemplateMessage(
        name="order_update",
        language="en_US",
        params=["Asha", "#1042"],
        header_media_url="https://cdn.example.com/a.jpg",
        buttons=[
            {"sub_type": "url", "index": 0, "text": "orders/1042"},
            {"sub_type": "quick_reply", "index": 1, "payload": "stop"},
            {"sub_type": "copy_code", "index": 2, "coupon_code": "SAVE10"},
        ],
    )
    p.send(SENDER, Outbound(kind="template", to="15550101", template=tm))
    body = json.loads(seen[0].content)
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550101",
        "type": "template",
        "template": {
            "name": "order_update",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "header",
                    "parameters": [{"type": "image", "image": {"link": "https://cdn.example.com/a.jpg"}}],
                },
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": "Asha"}, {"type": "text", "text": "#1042"}],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": "orders/1042"}],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "1",
                    "parameters": [{"type": "payload", "payload": "stop"}],
                },
                {
                    "type": "button",
                    "sub_type": "copy_code",
                    "index": "2",
                    "parameters": [{"type": "coupon_code", "coupon_code": "SAVE10"}],
                },
            ],
        },
    }


def test_template_without_params_and_document_header() -> None:
    bare = build_message_body(Outbound(kind="template", to="1", template=TemplateMessage(name="hello_world")))
    assert bare["template"] == {"name": "hello_world", "language": {"code": "en"}}
    doc = build_message_body(
        Outbound(
            kind="template",
            to="1",
            template=TemplateMessage(
                name="invoice", header_media_id="MEDIA1", header_document_name="inv.pdf"
            ),
        )
    )
    assert doc["template"]["components"][0] == {
        "type": "header",
        "parameters": [{"type": "document", "document": {"id": "MEDIA1", "filename": "inv.pdf"}}],
    }
    with pytest.raises(ValueError):
        build_message_body(Outbound(kind="template", to="1"))


def test_constructor_overrides() -> None:
    p, seen = make(ok_send, base_url="https://other.invalid/", api_version="v1.0")
    p.send(SENDER, Outbound(kind="text", to="1", text="x"))
    assert str(seen[0].url) == "https://other.invalid/v1.0/PN1/messages"
    p.close()


@pytest.mark.parametrize(
    ("status", "body", "headers", "exc", "code", "retryable"),
    [
        (
            429,
            {"error": {"code": 130429, "message": "slow down"}},
            {"Retry-After": "2"},
            RateLimited,
            "rate_limited",
            True,
        ),
        (400, {"error": {"code": 131056, "message": "pair rate"}}, {}, RateLimited, "rate_limited", True),
        (
            400,
            {
                "error": {
                    "code": 131047,
                    "message": "re-engagement",
                    "error_data": {"details": "window closed"},
                }
            },
            {},
            ProviderError,
            "131047",
            False,
        ),
        (500, {"error": {"code": 2, "message": "temporary"}}, {}, ProviderError, "2", True),
        (503, {}, {}, ProviderError, "503", True),
        (401, {"error": {"code": 190, "message": "token expired"}}, {}, ProviderError, "190", False),
    ],
)
def test_error_mapping(
    status: int,
    body: dict[str, Any],
    headers: dict[str, str],
    exc: type[Exception],
    code: str,
    retryable: bool,
) -> None:
    p, _ = make(lambda r: httpx.Response(status, json=body, headers=headers))
    with pytest.raises(exc) as info:
        p.send(SENDER, Outbound(kind="text", to="1", text="x"))
    err = info.value
    assert isinstance(err, ProviderError)
    assert err.code == code and err.retryable is retryable and err.http_status == status
    if isinstance(err, RateLimited) and headers:
        assert err.retry_after == 2.0
    if code == "131047":
        assert str(err) == "window closed"


def test_non_json_error_body() -> None:
    p, _ = make(lambda r: httpx.Response(502, content=b"<html>bad gateway</html>"))
    with pytest.raises(ProviderError) as info:
        p.send(SENDER, Outbound(kind="text", to="1", text="x"))
    assert info.value.retryable and str(info.value) == "HTTP 502"


def test_network_errors() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    p, _ = make(refuse)
    with pytest.raises(ProviderError) as info:
        p.send(SENDER, Outbound(kind="text", to="1", text="x"))
    assert info.value.code == "network" and info.value.retryable

    p, _ = make(timeout)
    with pytest.raises(ProviderError) as info:
        p.send(SENDER, Outbound(kind="text", to="1", text="x"))
    assert info.value.code == "network_unknown" and not info.value.retryable


def test_create_template_request_shape() -> None:
    p, seen = make(
        lambda r: httpx.Response(200, json={"id": "987", "status": "PENDING", "category": "UTILITY"})
    )
    d = TemplateDefinition(
        name="order_update", category="utility", body="Hi {{1}}, order {{2}} is ready.", example=("Asha",)
    )
    res = p.create_template(
        "W1", "tok-1", name=d.name, language="en", category=d.category, components=d.components()
    )
    assert res == {"id": "987", "status": "pending"}
    req = seen[0]
    assert str(req.url) == f"{BASE}/v99.0/W1/message_templates"
    assert json.loads(req.content) == {
        "name": "order_update",
        "language": "en",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": "Hi {{1}}, order {{2}} is ready.",
                "example": {"body_text": [["Asha", "sample 2"]]},
            }
        ],
    }


def test_create_template_errors() -> None:
    p, _ = make(lambda r: httpx.Response(400, json={"error": {"code": 100, "error_user_msg": "Name taken"}}))
    with pytest.raises(ProviderError) as info:
        p.create_template("W1", "t", name="x", language="en", category="utility", components=[])
    assert info.value.code == "template_create_failed" and "Name taken (code 100)" in str(info.value)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    p, _ = make(boom)
    with pytest.raises(ProviderError):
        p.create_template("W1", "t", name="x", language="en", category="utility", components=[])
    with pytest.raises(ProviderError):
        p.list_templates("W1", "t")


def test_list_templates_follows_paging() -> None:
    page2 = f"{BASE}/v99.0/W1/message_templates?after=CURSOR"

    def handler(request: httpx.Request) -> httpx.Response:
        if "after=CURSOR" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "2",
                            "name": "b",
                            "status": "REJECTED",
                            "category": "MARKETING",
                            "language": "en",
                            "rejected_reason": "PROMOTIONAL",
                        }
                    ]
                },
            )
        assert request.url.params["fields"] == "id,name,status,category,language,rejected_reason,components"
        assert request.url.params["limit"] == "200"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "name": "a",
                        "status": "APPROVED",
                        "category": "UTILITY",
                        "language": "en",
                        "rejected_reason": "NONE",
                        "components": [
                            {"type": "HEADER", "format": "TEXT"},
                            {"type": "BODY", "text": "Hi {{1}}"},
                        ],
                    }
                ],
                "paging": {"next": page2},
            },
        )

    p, seen = make(handler)
    out = p.list_templates("W1", "tok-1")
    assert [t["name"] for t in out] == ["a", "b"]
    assert out[0] == {
        "id": "1",
        "name": "a",
        "status": "approved",
        "category": "utility",
        "language": "en",
        "reason": "",
        "body": "Hi {{1}}",
    }
    assert out[1]["reason"] == "PROMOTIONAL"
    assert len(seen) == 2

    p, _ = make(lambda r: httpx.Response(403, json={"error": {"message": "denied", "code": 10}}))
    with pytest.raises(ProviderError) as info:
        p.list_templates("W1", "t")
    assert info.value.code == "template_list_failed"


def test_parse_webhook_all_kinds() -> None:
    payload = {
        "entry": [
            {
                "id": "W1",
                "time": 1700000000,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PN1"},
                            "messages": [
                                {
                                    "from": "15550101",
                                    "id": "m1",
                                    "timestamp": "1",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                },
                                {
                                    "from": "15550101",
                                    "id": "m2",
                                    "type": "button",
                                    "button": {"text": "Stop promotions", "payload": "stop"},
                                },
                                {
                                    "from": "15550101",
                                    "id": "m3",
                                    "type": "interactive",
                                    "interactive": {"button_reply": {"id": "yes", "title": "Yes"}},
                                },
                                {
                                    "from": "15550101",
                                    "id": "m4",
                                    "type": "image",
                                    "image": {"caption": "look"},
                                },
                                {"from": "15550101", "id": "m5", "type": "sticker", "timestamp": "bad"},
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.X",
                                    "status": "failed",
                                    "recipient_id": "15550101",
                                    "timestamp": "2",
                                    "errors": [{"code": 131047}],
                                }
                            ],
                        },
                    },
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": "APPROVED",
                            "message_template_id": 55,
                            "message_template_name": "a",
                            "message_template_language": "en",
                            "reason": "NONE",
                        },
                    },
                    {
                        "field": "template_category_update",
                        "value": {
                            "message_template_id": 55,
                            "message_template_name": "a",
                            "message_template_language": "en",
                            "previous_category": "UTILITY",
                            "new_category": "MARKETING",
                        },
                    },
                    {
                        "field": "phone_number_quality_update",
                        "value": {
                            "display_phone_number": "15550100",
                            "event": "FLAGGED",
                            "current_limit": "TIER_1K",
                        },
                    },
                ],
            }
        ]
    }
    events = parse_webhook_payload(payload)
    kinds = [e.kind for e in events]
    assert kinds == ["inbound"] * 5 + ["status", "template_status", "template_category", "quality"]
    text, button, inter, image, sticker = events[:5]
    assert (text.event_id, text.phone, text.text, text.phone_number_id, text.waba_id) == (
        "in:m1",
        "15550101",
        "hi",
        "PN1",
        "W1",
    )
    assert (button.text, button.button_payload) == ("Stop promotions", "stop")
    assert (inter.text, inter.button_payload) == ("Yes", "yes")
    assert image.text == "look" and sticker.text == "" and sticker.timestamp == 0
    st = events[5]
    assert (st.event_id, st.status, st.error_code, st.provider_message_id) == (
        "st:wamid.X:failed",
        "failed",
        "131047",
        "wamid.X",
    )
    tpl = events[6]
    assert (tpl.template_id, tpl.template_status, tpl.template_name) == ("55", "approved", "a")
    cat = events[7]
    assert (cat.category, cat.reason) == ("marketing", "utility -> marketing")
    q = events[8]
    assert (q.phone, q.quality, q.tier) == ("15550100", "flagged", "TIER_1K")
    assert parse_webhook_payload({}) == []


def test_engine_with_meta_provider_end_to_end() -> None:
    """The engine driving the real adapter, with the transport mocked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/message_templates") and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/message_templates"):
            return httpx.Response(200, json={"id": "1", "status": "APPROVED"})
        return ok_send(request)

    p, seen = make(handler)
    storage = Storage("sqlite://", table_prefix="meta_")
    storage.create_all()
    engine = Engine(storage, p, Settings(provider="meta_cloud", sending_enabled=True, token_key=""))
    engine.add_number(tenant_id="t1", phone_number_id="PN1", waba_id="W1", token="tok-own")
    engine.submit_templates(
        "W1", [TemplateDefinition(name="hello", category="utility", body="Hi {{1}}, welcome.")]
    )
    res = engine.send(tenant_id="t1", to="+1 555 0101", text="Sam", template="hello")
    assert res.ok and res.provider_message_id == "wamid.ABC"
    send_req = seen[-1]
    assert send_req.headers["Authorization"] == "Bearer tok-own"
    assert json.loads(send_req.content)["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Sam"}
    ]


def test_real_provider_is_paused_by_default() -> None:
    p, seen = make(ok_send)
    storage = Storage("sqlite://")
    storage.create_all()
    engine = Engine(storage, p, Settings(provider="meta_cloud"))
    engine.add_number(tenant_id="t1", phone_number_id="PN1", token="tok")
    res = engine.send_template(tenant_id="t1", to="15550101", template=TemplateMessage(name="hello_world"))
    assert res.skipped == "sending_paused" and seen == []
