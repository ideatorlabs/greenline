"""End to end: the example FastAPI app driven over HTTP with signed, Meta-shaped webhooks."""

from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from greenline import Engine, MockProvider, testing
from greenline.storage import utcnow

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CUSTOMER = "15550101"


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("greenline_example_app", EXAMPLES / "fastapi_app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example() -> ModuleType:
    return load_example()


class Harness:
    def __init__(self, example: ModuleType) -> None:
        self.ex = example
        self.provider = MockProvider()
        self.app = example.create_app(provider=self.provider)
        self.engine: Engine = self.app.state.engine
        self.events: list[dict[str, Any]] = self.app.state.events
        self.client = TestClient(self.app)

    def webhook(self, payload: dict[str, Any], secret: str | None = None) -> Any:
        raw, headers = testing.signed(payload, self.ex.APP_SECRET if secret is None else secret)
        return self.client.post("/webhooks/whatsapp", content=raw, headers=headers)

    def inbound(self, text: str, **kw: Any) -> Any:
        return self.webhook(
            testing.inbound_payload(phone_number_id=self.ex.PHONE_NUMBER_ID, phone=CUSTOMER, text=text, **kw)
        )

    def status(self, provider_message_id: str, status: str, **kw: Any) -> Any:
        return self.webhook(
            testing.status_payload(
                phone_number_id=self.ex.PHONE_NUMBER_ID,
                provider_message_id=provider_message_id,
                status=status,
                phone=CUSTOMER,
                **kw,
            )
        )

    def send(self, **body: Any) -> dict[str, Any]:
        r = self.client.post(f"/tenants/{self.ex.TENANT}/messages", json={"to": CUSTOMER, **body})
        assert r.status_code == 200, r.text
        return dict(r.json())

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


def test_full_flow(example: ModuleType) -> None:
    h = Harness(example)

    # 1. webhook verification handshake
    ok = h.client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": example.VERIFY_TOKEN,
            "hub.challenge": "challenge-42",
        },
    )
    assert (ok.status_code, ok.text) == (200, "challenge-42")
    wrong = h.client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"},
    )
    assert wrong.status_code == 403

    # 2. a signed inbound message fires the inbound hook after commit
    seen_in_db: list[Any] = []

    def check_committed(message_id: str, **_: Any) -> None:
        seen_in_db.append(h.engine.message(message_id))

    h.engine.hooks.on("inbound", check_committed)
    r = h.inbound("Hello!")
    assert r.status_code == 200 and r.json() == {"processed": 1}
    assert h.names() == ["inbound"]
    first = h.events[0]
    assert first["tenant_id"] == example.TENANT
    assert first["phone"] == CUSTOMER and first["text"] == "Hello!"
    assert seen_in_db[0] is not None and seen_in_db[0]["direction"] == "in"

    # a retried delivery of the same webhook item is processed once
    payload = testing.inbound_payload(
        phone_number_id=example.PHONE_NUMBER_ID, phone=CUSTOMER, text="again", message_id="wamid.retry.1"
    )
    assert h.webhook(payload).json() == {"processed": 1}
    assert h.webhook(payload).json() == {"processed": 0}

    # 3. STOP opts out, and sending is refused
    h.inbound("STOP")
    assert h.names()[-2:] == ["opt_out", "inbound"]
    refused = h.send(text="We miss you", template="order_update")
    assert refused["ok"] is False and refused["skipped"] == "opted_out"
    assert len(h.provider.sent) == 0

    # 4. START opts back in
    h.inbound("Start")
    assert h.names()[-2:] == ["opt_in", "inbound"]

    # 5. inside the 24h window, free-form text goes out
    text = h.send(text="Welcome back!", template="order_update", idempotency_key="welcome-1")
    assert text["ok"] and text["kind"] == "text" and text["status"] == "sent"
    assert h.provider.last.kind == "text" and h.provider.last.text == "Welcome back!"

    # the same idempotency key does not send twice
    again = h.send(text="Welcome back!", template="order_update", idempotency_key="welcome-1")
    assert again["skipped"] == "duplicate" and again["message_id"] == text["message_id"]
    assert len(h.provider.sent) == 1

    # 6. outside the window the approved template goes out, params sanitised and fitted
    h.engine.clock = lambda: utcnow() + timedelta(hours=25)
    try:
        tpl = h.send(
            text="ignored outside the window",
            template="order_update",
            params=["Asha\nKumar", "Demo   \t  Cafe", "Ready", "extra"],
        )
        assert tpl["ok"] and tpl["kind"] == "template" and tpl["template_name"] == "order_update"
        sent = h.provider.last
        assert sent.kind == "template" and sent.template is not None
        assert sent.template.params == ["Asha Kumar", "Demo Cafe", "Ready"]
        # free-form text alone is refused outside the window
        alone = h.send(text="just text")
        assert alone["skipped"] == "outside_window"
    finally:
        h.engine.clock = utcnow

    # 7. receipts move forward, never backward; failed cannot replace delivered
    mid = text["provider_message_id"]
    h.status(mid, "delivered")
    assert h.engine.message(text["message_id"])["status"] == "delivered"
    h.status(mid, "read")
    status_events = [e for e in h.events if e["event"] == "status"]
    h.status(mid, "delivered")
    h.status(mid, "failed", error_code="131047")
    assert h.engine.message(text["message_id"])["status"] == "read"
    assert [e["status"] for e in h.events if e["event"] == "status"] == ["delivered", "read"]
    assert status_events[-1]["tenant_id"] == example.TENANT and status_events[-1]["phone"] == CUSTOMER

    # a failure on a merely-sent message is applied, and its cost is dropped
    tpl_id = tpl["message_id"]
    assert h.engine.message(tpl_id)["cost_minor"] == 12
    h.status(tpl["provider_message_id"], "failed", error_code="131026")
    failed = h.engine.message(tpl_id)
    assert failed["status"] == "failed" and failed["error_code"] == "131026" and failed["cost_minor"] == 0

    # 8. a bad or missing signature is rejected and nothing is processed
    before = len(h.events)
    bad = h.webhook(
        testing.inbound_payload(phone_number_id=example.PHONE_NUMBER_ID, phone=CUSTOMER, text="x"), "wrong"
    )
    assert bad.status_code == 403
    missing = h.client.post("/webhooks/whatsapp", content=b"{}")
    assert missing.status_code == 403
    assert len(h.events) == before

    # malformed but signed bodies are acknowledged (so Meta does not retry) and ignored
    raw = b"not json"
    from greenline import sign

    junk = h.client.post(
        "/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sign(example.APP_SECRET, raw)}
    )
    assert junk.status_code == 200 and junk.json() == {"processed": 0}

    usage = h.client.get(f"/tenants/{example.TENANT}/usage").json()
    assert usage == {"messages": 1, "cost_minor": 0}  # the text; the failed template is not counted
    assert h.client.get("/events").json()[0]["event"] == "inbound"


def test_demo_script_runs(example: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    lines = example.run_demo()
    assert lines[0] == "handshake: 200 12345"
    assert "after STOP, send: opted_out" in lines
    assert "after START, inside the window: text (sent)" in lines
    assert "25 hours later, outside the window: template order_update" in lines
    assert "receipts delivered, read, delivered -> stored status: read" in lines
    assert "bad signature: 403" in lines
    assert "handshake" in capsys.readouterr().out


def test_module_level_app_serves(example: ModuleType) -> None:
    client = TestClient(example.app)
    r = client.get(f"/tenants/{example.TENANT}/usage")
    assert r.json() == {"messages": 0, "cost_minor": 0}
