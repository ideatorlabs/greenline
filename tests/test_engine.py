"""Engine behaviour: send rules, retries, idempotency, inbound processing, attribution, metering."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from greenline import (
    Engine,
    MockProvider,
    NumberNotReady,
    ProviderError,
    RateCard,
    RateLimited,
    TemplateMessage,
    WebhookEvent,
    status_advances,
    testing,
)

from .conftest import Clock

PHONE = "15550101"


def inbound(engine: Engine, text: str, phone: str = PHONE, number: str = "PN1", **kw: Any) -> int:
    return engine.process_webhook(
        testing.inbound_payload(phone_number_id=number, phone=phone, text=text, **kw)
    )


def capture(engine: Engine, *names: str) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        engine.hooks.on(name, lambda _n=name, **kw: seen.append((_n, kw)))
    return seen


# -- input checks and sender resolution ------------------------------------------------------


def test_input_checks(engine: Engine) -> None:
    assert engine.send(tenant_id="t1", to="", text="x").skipped == "no_phone"
    assert engine.send(tenant_id="t1", to=PHONE, text="  ").skipped == "empty_message"
    assert engine.send_text(tenant_id="t1", to="abc", text="x").skipped == "no_phone"
    assert engine.send_text(tenant_id="t1", to=PHONE, text=" ").skipped == "empty_message"
    assert (
        engine.send_template(tenant_id="t1", to="", template=TemplateMessage(name="n")).skipped == "no_phone"
    )


def test_no_number_raises(make_engine: Callable[..., Engine]) -> None:
    e = make_engine()
    with pytest.raises(NumberNotReady):
        e.send(tenant_id="nobody", to=PHONE, text="x")


def test_own_number_beats_shared_and_shared_is_fallback(make_engine: Callable[..., Engine]) -> None:
    e = make_engine(fallback_token="sys-token")
    e.add_number(phone_number_id="SHARED", waba_id="WS")  # no tenant: shared, no own token
    e.add_number(tenant_id="t1", phone_number_id="OWN", token="own-token", waba_id="W1")
    own = e.sender_for("t1")
    assert (own.phone_number_id, own.token) == ("OWN", "own-token")
    shared = e.sender_for("t2")
    assert (shared.phone_number_id, shared.token) == ("SHARED", "sys-token")
    e.disable_number("OWN")
    e.disable_number("missing")  # no-op
    assert e.sender_for("t1").phone_number_id == "SHARED"
    listed = e.numbers()
    assert {n["phone_number_id"]: n["shared"] for n in listed} == {"SHARED": True, "OWN": False}
    assert [n["phone_number_id"] for n in e.numbers("t1")] == ["OWN"]
    # re-registering updates in place
    e.add_number(tenant_id="t1", phone_number_id="OWN", token="new", waba_id="W1")
    assert e.sender_for("t1").token == "new"


def test_tokens_encrypted_at_rest(make_engine: Callable[..., Engine]) -> None:
    from greenline import generate_key

    e = make_engine(token_key=generate_key())
    e.add_number(tenant_id="t1", phone_number_id="PN9", token="secret-token")
    with e.storage.session() as db:
        stored = db.query(e.m.Number).one().token_enc
    assert stored.startswith("enc:") and "secret-token" not in stored
    assert e.sender_for("t1").token == "secret-token"


# -- gate -------------------------------------------------------------------------------------


def test_master_switch(make_engine: Callable[..., Engine], provider: MockProvider) -> None:
    e = make_engine(sending_enabled=False)
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    r = e.send_template(tenant_id="t1", to=PHONE, template=TemplateMessage(name="x"))
    assert r.skipped == "sending_paused" and provider.sent == []


def test_test_mode_allow_list(make_engine: Callable[..., Engine], provider: MockProvider) -> None:
    e = make_engine(
        test_mode=True, test_recipients=("+1 555 0199",), test_recipients_for=lambda t: ["15550142"]
    )
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    tm = TemplateMessage(name="x")
    assert e.send_template(tenant_id="t1", to=PHONE, template=tm).skipped == "test_mode"
    assert e.send_template(tenant_id="t1", to="15550199", template=tm).ok
    assert e.send_template(tenant_id="t1", to="+1-555-0142", template=tm).ok
    assert len(provider.sent) == 2


# -- consent ----------------------------------------------------------------------------------


def test_marketing_needs_opt_in(engine: Engine, provider: MockProvider) -> None:
    r = engine.send(tenant_id="t1", to=PHONE, text="Sale", template="promo")
    assert r.skipped == "not_opted_in"
    assert engine.record_opt_in("t1", PHONE, source="signup-form", external_id="cust-1") is True
    state = engine.contact("t1", PHONE)
    assert state is not None and state["opted_in"] and state["opt_in_source"] == "signup-form"
    r = engine.send(tenant_id="t1", to=PHONE, text="Sale", template="promo")
    assert r.ok and r.kind == "template"
    assert engine.contact("t1", "15550199") is None


def test_marketing_warmup_setting(make_engine: Callable[..., Engine]) -> None:
    e = make_engine(allow_marketing_without_opt_in=True)
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    tm = TemplateMessage(name="promo", category="marketing")
    assert e.send_template(tenant_id="t1", to=PHONE, template=tm).ok


def test_stop_beats_later_opt_in_and_keywords_are_whole_message(engine: Engine) -> None:
    seen = capture(engine, "opt_out", "opt_in")
    inbound(engine, "I want nonstop updates")
    inbound(engine, "please restart")
    assert seen == []
    inbound(engine, "  Stop! ")
    assert [n for n, _ in seen] == ["opt_out"]
    assert engine.record_opt_in("t1", PHONE) is False
    assert engine.send(tenant_id="t1", to=PHONE, text="hi").skipped == "opted_out"
    assert engine.send_text(tenant_id="t1", to=PHONE, text="hi").skipped == "opted_out"
    inbound(engine, "OPT-IN")
    assert [n for n, _ in seen] == ["opt_out", "opt_in"]
    assert engine.send(tenant_id="t1", to=PHONE, text="hi").ok


def test_stop_button_payload(engine: Engine) -> None:
    seen = capture(engine, "opt_out")
    inbound(engine, "Stop promotions", button_payload="stop:campaign-7")
    assert seen and seen[0][1]["tenant_id"] == "t1"
    assert engine.contact("t1", PHONE)["opted_out"] is True  # type: ignore[index]


def test_inbound_opt_in_setting(make_engine: Callable[..., Engine]) -> None:
    e = make_engine(inbound_counts_as_opt_in=False)
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    inbound(e, "hello")
    state = e.contact("t1", PHONE)
    assert state is not None and state["opted_in"] is False and state["in_window"] is True


def test_window_expires(engine: Engine, clock: Clock) -> None:
    inbound(engine, "hi")
    assert engine.send(tenant_id="t1", to=PHONE, text="reply").kind == "text"
    clock.advance(hours=24, seconds=1)
    assert engine.send(tenant_id="t1", to=PHONE, text="reply").skipped == "outside_window"
    assert engine.send_text(tenant_id="t1", to=PHONE, text="reply").skipped == "outside_window"
    r = engine.send(tenant_id="t1", to=PHONE, text="reply", template="notice")
    assert r.kind == "template"


# -- templates on send ------------------------------------------------------------------------


def test_template_resolution_and_params(engine: Engine, provider: MockProvider) -> None:
    r = engine.send(tenant_id="t1", to=PHONE, text="fallback", template="missing")
    assert r.skipped == "no_approved_template:missing"
    r = engine.send(tenant_id="t1", to=PHONE, text="Line\twith  tabs")
    assert r.skipped == "outside_window"
    r = engine.send(tenant_id="t1", to=PHONE, text="Your table is ready", template="notice")
    assert r.ok
    assert provider.last.template is not None and provider.last.template.params == [
        "Your table is ready",
        "-",
    ]
    with engine.storage.session() as db:
        msg = db.get(engine.m.Message, r.message_id)
        assert msg.body == "Your table is ready" and msg.template_name == "notice"


def test_template_message_passthrough(engine: Engine, provider: MockProvider) -> None:
    tm = TemplateMessage(name="custom", params=["a\nb", ""], param_count=None)
    r = engine.send(tenant_id="t1", to=PHONE, template=tm)
    assert r.ok and provider.last.template is not None and provider.last.template.params == ["a b", "-"]
    tm2 = TemplateMessage(name="custom", param_count=3)
    engine.send(tenant_id="t1", to=PHONE, template=tm2, params=["x"])
    assert provider.last.template is not None and provider.last.template.params == ["x", "-", "-"]
    # a template with no variables records its name as the body
    engine.send_template(tenant_id="t1", to=PHONE, template=TemplateMessage(name="hello_world"))
    with engine.storage.session() as db:
        bodies = [m.body for m in db.query(engine.m.Message).all()]
    assert "hello_world" in bodies


# -- idempotency and retries ------------------------------------------------------------------


def test_idempotency(engine: Engine, provider: MockProvider) -> None:
    tm = TemplateMessage(name="notice", param_count=2, params=["a"])
    first = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k1")
    second = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k1")
    assert first.ok and second.skipped == "duplicate" and second.ok
    assert second.message_id == first.message_id and len(provider.sent) == 1


def test_failed_send_frees_key_but_unknown_delivery_keeps_it(engine: Engine, provider: MockProvider) -> None:
    tm = TemplateMessage(name="notice")
    provider.failures = [ProviderError("bad number", code="131026")]
    failed = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k2")
    assert not failed.ok and failed.status == "failed" and failed.error_code == "131026"
    retry = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k2")
    assert retry.ok and retry.skipped == ""

    provider.failures = [ProviderError("timeout", code="network_unknown")]
    unknown = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k3")
    assert not unknown.ok
    again = engine.send_template(tenant_id="t1", to=PHONE, template=tm, idempotency_key="k3")
    assert again.skipped == "duplicate" and not again.ok


def test_retries_with_backoff_within_budget(
    make_engine: Callable[..., Engine], provider: MockProvider
) -> None:
    waits: list[float] = []
    e = make_engine()
    e._sleep = waits.append
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    tm = TemplateMessage(name="x")
    provider.failures = [ProviderError("busy", code="2", retryable=True), RateLimited(retry_after=1.5)]
    assert e.send_template(tenant_id="t1", to=PHONE, template=tm).ok
    assert waits == [0.5, 1.5]

    waits.clear()
    provider.failures = [RateLimited(retry_after=60)]
    r = e.send_template(tenant_id="t1", to=PHONE, template=tm)
    assert not r.ok and r.error_code == "rate_limited" and waits == []

    provider.failures = [ProviderError("busy", code="2", retryable=True)] * 5
    r = e.send_template(tenant_id="t1", to=PHONE, template=tm)
    assert not r.ok and r.error_code == "2" and waits == [0.5, 1.0, 2.0]


def test_expired_token_falls_back(make_engine: Callable[..., Engine], provider: MockProvider) -> None:
    e = make_engine(fallback_token="sys")
    e.add_number(tenant_id="t1", phone_number_id="PN1", token="own")
    provider.failures = [ProviderError("expired", code="190")]
    r = e.send_template(tenant_id="t1", to=PHONE, template=TemplateMessage(name="x"))
    assert r.ok and provider.sent[-1][0].token == "sys"

    provider.failures = [ProviderError("expired", code="190"), ProviderError("expired", code="190")]
    r = e.send_template(tenant_id="t1", to=PHONE, template=TemplateMessage(name="x"))
    assert not r.ok and "reconnect" in r.error


# -- number protection and metering -----------------------------------------------------------


def test_daily_cap(make_engine: Callable[..., Engine], clock: Clock) -> None:
    e = make_engine(daily_cap_per_number=2)
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    tm = TemplateMessage(name="x")
    assert e.send_template(tenant_id="t1", to="15550101", template=tm).ok
    assert e.send_template(tenant_id="t1", to="15550102", template=tm).ok
    assert e.send_template(tenant_id="t1", to="15550103", template=tm).skipped == "daily_cap"
    clock.advance(hours=25)
    assert e.send_template(tenant_id="t1", to="15550103", template=tm).ok


def test_quality_red_holds_marketing(engine: Engine) -> None:
    seen = capture(engine, "quality")
    engine.record_opt_in("t1", PHONE)
    engine.process_webhook(
        testing.quality_payload(
            waba_id="W1", display_phone="15550100", event="FLAGGED", current_limit="TIER_250"
        )
    )
    assert seen[0][1]["quality"] == "flagged"
    num = engine.numbers("t1")[0]
    assert (num["quality"], num["tier"]) == ("red", "TIER_250")
    assert engine.send(tenant_id="t1", to=PHONE, text="Sale", template="promo").skipped == "quality_paused"
    assert engine.send(tenant_id="t1", to=PHONE, text="Order", template="notice").ok
    engine.process_webhook(
        testing.quality_payload(waba_id="W1", display_phone="+1 555 0100", event="UNFLAGGED")
    )
    assert engine.numbers("t1")[0]["quality"] == "green"
    assert engine.send(tenant_id="t1", to=PHONE, text="Sale", template="promo").ok
    # events with no phone, or for another number, change nothing
    engine.process_events([WebhookEvent(kind="quality", event_id="q-empty")])
    engine.process_webhook(testing.quality_payload(waba_id="", display_phone="15550177", event="FLAGGED"))
    assert engine.numbers("t1")[0]["quality"] == "green"


def test_metering_and_budget(make_engine: Callable[..., Engine]) -> None:
    card = RateCard(
        currency="INR",
        rates={"marketing": 100, "utility": 10, "service": 5},
        free_service_per_number_per_month=1,
    )
    e = make_engine(rate_card=card, monthly_budget_minor=150, allow_marketing_without_opt_in=True)
    e.add_number(tenant_id="t1", phone_number_id="PN1")
    e.add_number(tenant_id="t2", phone_number_id="PN2")
    promo = TemplateMessage(name="p", category="marketing")
    assert e.send_template(tenant_id="t1", to="15550101", template=promo).ok
    assert e.send_template(tenant_id="t2", to="15550102", template=TemplateMessage(name="u")).ok
    assert e.send_template(tenant_id="t2", to="15550102", template=promo).ok  # spent 110 < 150 before
    assert e.send_template(tenant_id="t1", to="15550103", template=promo).skipped == "budget_exhausted"
    assert e.send_template(
        tenant_id="t1", to="15550103", template=TemplateMessage(name="u")
    ).ok  # utility still goes
    inbound(e, "hi", phone="15550101")
    inbound(e, "hi again", phone="15550101")
    e.send(tenant_id="t1", to="15550101", text="first reply is free")
    e.send(tenant_id="t1", to="15550101", text="second is billed")
    assert e.usage() == {"t1": {"messages": 4, "cost_minor": 115}, "t2": {"messages": 2, "cost_minor": 110}}


# -- inbound ----------------------------------------------------------------------------------


def test_hooks_fire_after_commit_and_failures_are_isolated(engine: Engine) -> None:
    def broken(**_: Any) -> None:
        raise RuntimeError("subscriber bug")

    rows: list[Any] = []
    engine.hooks.on("inbound", broken)
    engine.hooks.on("inbound", lambda message_id, **_: rows.append(engine.message(message_id)))
    assert inbound(engine, "hello") == 1
    assert rows[0]["body"] == "hello" and rows[0]["status"] == "received"


def test_shared_number_attribution(make_engine: Callable[..., Engine], clock: Clock) -> None:
    e = make_engine(attribution_days=30)
    e.add_number(phone_number_id="SHARED")
    seen = capture(e, "inbound", "opt_out")
    tm = TemplateMessage(name="x")
    e.send_template(tenant_id="alpha", to=PHONE, template=tm, external_id="alpha-cust-1")
    clock.advance(minutes=5)
    e.send_template(tenant_id="beta", to=PHONE, template=tm, external_id="beta-cust-9")
    clock.advance(minutes=5)
    inbound(e, "who is this?", number="SHARED")
    assert seen[-1][1]["tenant_id"] == "beta"
    assert seen[-1][1]["external_id"] is None  # the contact row belongs to alpha
    e.send_template(tenant_id="alpha", to=PHONE, template=tm)
    inbound(e, "STOP", number="SHARED")
    assert seen[-2] == (
        "opt_out",
        {"tenant_id": "alpha", "phone": PHONE, "phone_number_id": "SHARED", "external_id": "alpha-cust-1"},
    )
    # past the attribution window nobody claims it
    clock.advance(days=31)
    inbound(e, "hello?", number="SHARED")
    assert seen[-1][1]["tenant_id"] == ""
    # a stranger who never got a message is unattributed too
    inbound(e, "hi", phone="15550177", number="SHARED")
    assert seen[-1][1]["tenant_id"] == ""


def test_tenant_resolver(make_engine: Callable[..., Engine]) -> None:
    def resolver(event: WebhookEvent) -> str | None:
        if "#CODE7" in event.text:
            return "gamma"
        if "boom" in event.text:
            raise ValueError("resolver bug")
        return None

    e = make_engine(tenant_resolver=resolver)
    e.add_number(phone_number_id="SHARED")
    seen = capture(e, "inbound")
    inbound(e, "Hi #CODE7", number="SHARED")
    inbound(e, "boom", number="SHARED", phone="15550102")
    assert [p["tenant_id"] for _, p in seen] == ["gamma", ""]


def test_duplicate_events_and_unknown_kinds(engine: Engine) -> None:
    ev = WebhookEvent(kind="unknown", event_id="u1")
    assert engine.process_events([ev, ev]) == 1


def test_status_rules(engine: Engine) -> None:
    seen = capture(engine, "status")
    r = engine.send(tenant_id="t1", to=PHONE, text="x", template="notice")
    mid = r.provider_message_id

    def receipt(status: str, error: str = "") -> None:
        engine.process_webhook(
            testing.status_payload(
                phone_number_id="PN1", provider_message_id=mid, status=status, error_code=error
            )
        )

    receipt("sent")  # same state: no change, no hook
    receipt("read")
    receipt("delivered")
    receipt("failed", "131047")
    assert engine.message(r.message_id)["status"] == "read"  # type: ignore[index]
    assert [p["status"] for _, p in seen] == ["read"]
    # a receipt for a message we did not send is passed through with no tenant
    engine.process_webhook(
        testing.status_payload(
            phone_number_id="PN1", provider_message_id="wamid.other", status="delivered", phone="15550199"
        )
    )
    assert seen[-1][1] == {
        "tenant_id": "",
        "phone": "15550199",
        "status": "delivered",
        "error_code": "",
        "provider_message_id": "wamid.other",
        "message_id": "",
    }


def test_status_advances_table() -> None:
    assert status_advances("queued", "sent") and status_advances("sent", "read")
    assert not status_advances("read", "delivered") and not status_advances("delivered", "failed")
    assert status_advances("sent", "failed") and not status_advances("failed", "read")
    assert not status_advances("sent", "weird") and not status_advances("received", "read")


def test_template_events_update_registry(engine: Engine, provider: MockProvider) -> None:
    seen = capture(engine, "template_status", "template_category")
    engine.process_webhook(
        testing.template_status_payload(waba_id="W1", name="promo", status="PAUSED", reason="LOW_QUALITY")
    )
    assert engine.templates.resolve("W1", "promo") is None
    assert seen[-1] == (
        "template_status",
        {"waba_id": "W1", "name": "promo", "language": "en", "status": "paused", "reason": "LOW_QUALITY"},
    )
    category_change = {
        "entry": [
            {
                "id": "W1",
                "time": 1,
                "changes": [
                    {
                        "field": "template_category_update",
                        "value": {
                            "message_template_name": "notice",
                            "message_template_language": "en",
                            "previous_category": "UTILITY",
                            "new_category": "MARKETING",
                        },
                    }
                ],
            }
        ]
    }
    engine.process_webhook(category_change)
    resolved = engine.templates.resolve("W1", "notice")
    assert resolved is not None and resolved.category == "marketing"
    assert seen[-1][0] == "template_category"
    # now the re-filed template needs an opt-in like any marketing message
    assert engine.send(tenant_id="t1", to=PHONE, text="x", template="notice").skipped == "not_opted_in"
