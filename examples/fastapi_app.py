"""A complete Greenline app: FastAPI + SQLite + the mock provider.

Run the scripted walkthrough (no server, no network, nothing leaves your machine):

    python examples/fastapi_app.py

Or serve it and point a webhook simulator at it:

    uvicorn examples.fastapi_app:app --reload

Routes:
    GET/POST /webhooks/whatsapp            the webhook (handshake + signed events)
    POST     /tenants/{tenant_id}/messages send a message (text in the window, else the template)
    GET      /tenants/{tenant_id}/usage    messages and estimated cost this month
    GET      /events                       hook events received so far
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from greenline import Engine, MockProvider, RateCard, Settings, Storage, TemplateDefinition
from greenline.ext.fastapi import mount
from greenline.storage import utcnow

TENANT = "demo-tenant"
PHONE_NUMBER_ID = "100000000000001"
WABA_ID = "200000000000001"
APP_SECRET = "example-app-secret"
VERIFY_TOKEN = "example-verify-token"

ORDER_UPDATE = TemplateDefinition(
    name="order_update",
    category="utility",
    body="Hi {{1}}, an update from {{2}}: {{3}} Reply STOP to opt out.",
    example=("Asha", "Demo Cafe", "Your order is ready for pickup."),
)


class SendRequest(BaseModel):
    to: str
    text: str = ""
    template: str | None = None
    params: list[str] | None = None
    idempotency_key: str | None = None


def create_app(database_url: str = "sqlite://", provider: MockProvider | None = None) -> FastAPI:
    settings = Settings(provider="mock", app_secret=APP_SECRET, verify_token=VERIFY_TOKEN)
    storage = Storage(database_url)
    storage.create_all()
    engine = Engine(storage, provider or MockProvider(), settings, rate_card=RateCard.example_inr())

    engine.add_number(
        tenant_id=TENANT,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id=WABA_ID,
        token="example-access-token",
        display_phone="+1 555 0100",
        display_name="Demo Cafe",
    )
    engine.submit_templates(WABA_ID, [ORDER_UPDATE])  # the mock provider approves immediately

    events: list[dict[str, Any]] = []

    def recorder(name: str) -> Any:
        def record(**payload: Any) -> None:
            events.append({"event": name, **payload})

        return record

    for name in ("inbound", "status", "opt_out", "opt_in"):
        engine.hooks.on(name, recorder(name))

    app = FastAPI(title="Greenline example")
    app.state.engine = engine
    app.state.events = events
    mount(app, engine)

    @app.post("/tenants/{tenant_id}/messages")
    def send_message(tenant_id: str, body: SendRequest) -> dict[str, Any]:
        result = engine.send(
            tenant_id=tenant_id,
            to=body.to,
            text=body.text,
            template=body.template,
            params=body.params,
            idempotency_key=body.idempotency_key,
        )
        return asdict(result)

    @app.get("/tenants/{tenant_id}/usage")
    def usage(tenant_id: str) -> dict[str, int]:
        return engine.usage().get(tenant_id, {"messages": 0, "cost_minor": 0})

    @app.get("/events")
    def list_events() -> list[dict[str, Any]]:
        return events

    return app


app = create_app(os.environ.get("GREENLINE_DATABASE_URL", "sqlite://"))


def run_demo() -> list[str]:
    """Drive the whole flow through HTTP, the way Meta and your app would. Returns the log lines."""
    from fastapi.testclient import TestClient

    from greenline import testing

    demo = create_app()
    engine: Engine = demo.state.engine
    client = TestClient(demo)
    customer = "15550101"
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        print(line)

    def webhook(payload: dict[str, Any]) -> int:
        raw, headers = testing.signed(payload, APP_SECRET)
        return client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code

    def send(**body: Any) -> dict[str, Any]:
        return client.post(f"/tenants/{TENANT}/messages", json={"to": customer, **body}).json()

    r = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"},
    )
    say(f"handshake: {r.status_code} {r.text}")

    webhook(testing.inbound_payload(phone_number_id=PHONE_NUMBER_ID, phone=customer, text="Hi there"))
    say(f"inbound message -> hooks: {[e['event'] for e in demo.state.events]}")

    webhook(testing.inbound_payload(phone_number_id=PHONE_NUMBER_ID, phone=customer, text="STOP"))
    say(f"after STOP, send: {send(text='Your table is ready')['skipped']}")

    webhook(testing.inbound_payload(phone_number_id=PHONE_NUMBER_ID, phone=customer, text="start"))
    first = send(text="Welcome back!", template="order_update")
    say(f"after START, inside the window: {first['kind']} ({first['status']})")

    engine.clock = lambda: utcnow() + timedelta(hours=25)
    later = send(text="Your order is ready.", template="order_update", params=["Asha", "Demo Cafe"])
    say(f"25 hours later, outside the window: {later['kind']} {later['template_name']}")
    engine.clock = utcnow

    for status in ("delivered", "read", "delivered"):
        webhook(
            testing.status_payload(
                phone_number_id=PHONE_NUMBER_ID,
                provider_message_id=first["provider_message_id"],
                status=status,
            )
        )
    stored = engine.message(first["message_id"]) or {}
    say(f"receipts delivered, read, delivered -> stored status: {stored.get('status')}")

    raw, _ = testing.signed(
        testing.inbound_payload(phone_number_id=PHONE_NUMBER_ID, phone=customer, text="x"), ""
    )
    bad = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    say(f"bad signature: {bad.status_code}")

    say(f"usage this month: {client.get(f'/tenants/{TENANT}/usage').json()}")
    return lines


if __name__ == "__main__":
    run_demo()
