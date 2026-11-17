"""Small pieces: params, consent, security, settings, billing, hooks, registry, storage, webhook."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect

from greenline import (
    Button,
    Engine,
    Hooks,
    MockProvider,
    NotConfigured,
    ProviderError,
    RateCard,
    Settings,
    Storage,
    TemplateDefinition,
    TokenCipher,
    WebhookEvent,
    budget_status,
    build_provider,
    clean_param,
    create_all,
    fit_params,
    generate_key,
    keyword_action,
    normalise_status,
    register_provider,
    sign,
    verify_signature,
)
from greenline.config import DEFAULT_START_KEYWORDS, DEFAULT_STOP_KEYWORDS
from greenline.gate import SendGate, tail
from greenline.params import count_variables

# -- params -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hello", "hello"),
        ("line one\nline two", "line one line two"),
        ("a\r\n\tb", "a b"),
        ("too     many", "too many"),
        ("three   ok", "three   ok"),
        ("  padded  ", "padded"),
        ("", "-"),
        (None, "-"),
        ("\n\t", "-"),
        (42, "42"),
    ],
)
def test_clean_param(value: Any, expected: str) -> None:
    assert clean_param(value) == expected


def test_clean_param_caps_length() -> None:
    assert clean_param("x" * 2000) == "x" * 900
    assert clean_param("abc def", max_chars=4) == "abc"


def test_fit_params() -> None:
    assert fit_params(["a", "b", "c"], 2) == ["a", "b"]
    assert fit_params(["a"], 3) == ["a", "-", "-"]
    assert fit_params(None, 1) == ["-"]
    assert fit_params(["a"], 0) == []
    assert count_variables("Hi {{1}}, {{2}} and {{ 1 }}") == 2


# -- consent ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STOP", "stop"),
        (" stop! ", "stop"),
        ("Opt-Out", "stop"),
        ("unsubscribe.", "stop"),
        ("START", "start"),
        ("opt in", "start"),
        ("nonstop", None),
        ("please stop texting", None),
        ("restart", None),
        ("", None),
        ("!!!", None),
    ],
)
def test_keyword_action(text: str, expected: str | None) -> None:
    assert keyword_action(text, DEFAULT_STOP_KEYWORDS, DEFAULT_START_KEYWORDS) == expected


# -- security ---------------------------------------------------------------------------------


def test_signature() -> None:
    body = b'{"a":1}'
    header = sign("s3cret", body)
    assert verify_signature("s3cret", body, header)
    assert not verify_signature("s3cret", body + b" ", header)
    assert not verify_signature("other", body, header)
    assert not verify_signature("", body, header)  # fail closed without a secret
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "md5=abc")


def test_token_cipher() -> None:
    key = generate_key()
    c = TokenCipher(key)
    assert c.enabled
    enc = c.encrypt("tok")
    assert enc.startswith("enc:") and c.decrypt(enc) == "tok"
    assert c.encrypt("") == "" and c.decrypt("") == ""
    plain = TokenCipher()
    assert not plain.enabled
    assert plain.encrypt("tok") == "plain:tok" and plain.decrypt("plain:tok") == "tok"
    assert plain.decrypt("legacy-unprefixed") == "legacy-unprefixed"
    with pytest.raises(NotConfigured):
        plain.decrypt(enc)
    with pytest.raises(NotConfigured):
        TokenCipher(required=True).encrypt("tok")


# -- settings ---------------------------------------------------------------------------------


def test_settings_from_env() -> None:
    env = {
        "GREENLINE_PROVIDER": "meta_cloud",
        "GREENLINE_GRAPH_VERSION": "v30.0",
        "GREENLINE_HTTP_TIMEOUT": "4.5",
        "GREENLINE_MAX_RETRIES": "1",
        "GREENLINE_TEST_MODE": "yes",
        "GREENLINE_TEST_RECIPIENTS": "15550100, 15550101,",
        "GREENLINE_STOP_KEYWORDS": "STOP,Halt",
        "GREENLINE_SENDING_ENABLED": "",
        "GREENLINE_APP_SECRET": "abc",
    }
    s = Settings.from_env(environ=env)
    assert s.provider == "meta_cloud" and s.graph_version == "v30.0" and s.http_timeout == 4.5
    assert s.max_retries == 1 and s.test_mode is True
    assert s.test_recipients == ("15550100", "15550101")
    assert s.stop_keywords == ("stop", "halt")
    assert s.sending_enabled is None and s.sending_on is False  # real provider, switch unset: off
    assert s.app_secret == "abc"
    assert Settings.from_env(environ={"X_SENDING_ENABLED": "1"}, prefix="X_").sending_on is True
    assert Settings.from_env(environ={"GREENLINE_SENDING_ENABLED": "off"}).sending_on is False
    assert Settings().sending_on is True  # mock provider: on
    assert Settings().with_changes(provider="meta_cloud").sending_on is False


def test_gate_helpers() -> None:
    assert tail("+1 (555) 0100-123") == "5550100123"
    gate = SendGate(Settings())
    assert gate.check("1") is None and gate.recipient_allowed("") is True


# -- billing ----------------------------------------------------------------------------------


def test_rate_card() -> None:
    card = RateCard.example_inr()
    assert card.currency == "INR" and "verify current Meta pricing" in card.note
    assert card.cost("marketing") == 86
    assert card.cost("service", service_sent_this_month=0) == 0
    assert card.cost("service", service_sent_this_month=1000) == 12
    assert card.cost("unknown-category") == 12  # priced like utility
    assert card.to_major(86) == 0.86
    windowed = RateCard.from_dict({"currency": "USD", "rates": {"utility": 3}, "free_in_window": ["utility"]})
    assert windowed.cost("utility", in_window=True) == 0 and windowed.cost("utility") == 3


def test_budget_status() -> None:
    assert budget_status(80, 100) == {
        "spent": 80,
        "budget": 100,
        "share": 0.8,
        "warn": True,
        "exhausted": False,
    }
    assert budget_status(100, 100)["exhausted"] is True
    assert budget_status(5, 0) == {"spent": 5, "budget": 0, "share": 0.0, "warn": False, "exhausted": False}


# -- hooks ------------------------------------------------------------------------------------


def test_hooks() -> None:
    h = Hooks()
    got: list[Any] = []

    @h.on("inbound")
    def first(**kw: Any) -> None:
        got.append(kw)

    h.on("inbound", first)  # duplicate subscription ignored
    h.emit("inbound", text="hi")
    assert got == [{"text": "hi"}] and h.subscribers("inbound") == [first]
    h.off("inbound", first)
    h.off("inbound", first)  # no-op
    h.emit("inbound", text="again")
    assert len(got) == 1
    h.on("status", first)
    h.clear()
    assert h.subscribers("status") == []


# -- providers --------------------------------------------------------------------------------


def test_provider_registry() -> None:
    assert build_provider(Settings()).name == "mock"
    assert build_provider(Settings(provider="meta_cloud")).name == "meta_cloud"

    class Custom(MockProvider):
        name = "custom_bsp"

    register_provider("custom_bsp", lambda settings, **_: Custom())
    assert build_provider(Settings(provider="custom_bsp")).name == "custom_bsp"
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider(Settings(provider="nope"))


# -- storage ----------------------------------------------------------------------------------


def test_storage_prefix_and_create_all(tmp_path: Any) -> None:
    s = Storage(f"sqlite:///{tmp_path / 'c.db'}", table_prefix="msg_")
    create_all(s)
    s.create_all()  # idempotent
    names = set(inspect(s.engine).get_table_names())
    assert names == {"msg_numbers", "msg_contacts", "msg_messages", "msg_templates", "msg_webhook_events"}
    assert s.metadata is s.models.metadata
    s.drop_all()
    assert inspect(s.engine).get_table_names() == []
    shared = Storage(s.engine)
    assert shared.engine is s.engine and shared.table_prefix == "greenline_"


# -- template registry ------------------------------------------------------------------------


def test_template_definition_components() -> None:
    d = TemplateDefinition(
        name="offer",
        category="marketing",
        body="Hi {{1}}, {{2}} has a treat for you. Reply STOP to opt out.",
        example=("Asha",),
        footer="Sent by Sample Store",
        buttons=(
            Button("quick_reply", "Stop promotions"),
            Button("url", "View", url="https://example.com/o/{{1}}", example="abc"),
            Button("url", "Home", url="https://example.com"),
            Button("copy_code"),
        ),
    )
    assert d.param_count == 2
    assert d.components() == [
        {
            "type": "BODY",
            "text": "Hi {{1}}, {{2}} has a treat for you. Reply STOP to opt out.",
            "example": {"body_text": [["Asha", "sample 2"]]},
        },
        {"type": "FOOTER", "text": "Sent by Sample Store"},
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Stop promotions"},
                {"type": "URL", "text": "View", "url": "https://example.com/o/{{1}}", "example": ["abc"]},
                {"type": "URL", "text": "Home", "url": "https://example.com"},
                {"type": "COPY_CODE", "example": "CODE10"},
            ],
        },
    ]
    assert TemplateDefinition(name="x", category="utility", body="No variables.").components() == [
        {"type": "BODY", "text": "No variables."}
    ]


def test_normalise_status() -> None:
    assert normalise_status("APPROVED") == "approved"
    assert normalise_status("flagged") == "approved"
    assert normalise_status("IN_APPEAL") == "pending"
    assert normalise_status("deleted") == "disabled"
    assert normalise_status("whatever") is None


def test_registry_submit_is_idempotent(storage: Storage) -> None:
    provider = MockProvider(auto_approve=False)
    engine = Engine(storage, provider)
    engine.add_number(tenant_id="t1", phone_number_id="PN1", waba_id="W1", token="tok")
    defs = [
        TemplateDefinition(name="a", category="utility", body="Hi {{1}}, done."),
        TemplateDefinition(name="b", category="marketing", body="Hello {{1}} and {{2}}, see you."),
    ]
    first = engine.submit_templates("W1", defs)
    assert [(r["name"], r["status"], r["submitted"]) for r in first] == [
        ("a", "pending", True),
        ("b", "pending", True),
    ]
    assert engine.templates.resolve("W1", "a") is None  # pending is not sendable
    again = engine.submit_templates("W1", defs)
    assert [r["submitted"] for r in again] == [False, False]
    assert len(provider.submitted) == 2

    provider.set_template_status("W1", "a", "APPROVED")
    provider.set_template_status("W1", "b", "REJECTED")
    synced = engine.sync_templates("W1")
    assert {(r["name"], r["status"]) for r in synced} == {("a", "approved"), ("b", "rejected")}
    resolved = engine.templates.resolve("W1", "a", "fr")  # falls back to any language
    assert resolved is not None and resolved.param_count == 1 and resolved.category == "utility"
    listed = {t["name"]: t for t in engine.templates.list_templates("W1")}
    assert listed["a"]["sendable"] and not listed["b"]["sendable"]
    assert listed["b"]["param_count"] == 2

    # a rejected template is submitted again
    resubmitted = engine.submit_templates("W1", defs)
    assert [r["submitted"] for r in resubmitted] == [False, True]
    assert engine.templates.list_templates("W2") == []


def test_registry_submit_tolerates_provider_errors(storage: Storage) -> None:
    class Flaky(MockProvider):
        def list_templates(self, waba_id: str, token: str) -> list[dict[str, str]]:
            raise ProviderError("cannot list")

        def create_template(self, waba_id: str, token: str, **kw: Any) -> dict[str, str]:
            if kw["name"] == "bad":
                raise ProviderError("rejected at submit")
            return super().create_template(waba_id, token, **kw)

    engine = Engine(storage, Flaky(), Settings(fallback_token="sys"))
    out = engine.submit_templates(
        "W9",
        [
            TemplateDefinition(name="bad", category="utility", body="x"),
            TemplateDefinition(name="good", category="utility", body="y"),
        ],
    )
    assert out[0] == {
        "name": "bad",
        "status": "not_submitted",
        "submitted": False,
        "error": "rejected at submit",
    }
    assert out[1]["status"] == "approved"


def test_registry_apply_event_edges(storage: Storage) -> None:
    engine = Engine(storage, MockProvider())
    reg = engine.templates
    with storage.session() as db:
        assert reg.apply_event(db, WebhookEvent(kind="template_status", event_id="1")) is False
        assert (
            reg.apply_event(
                db,
                WebhookEvent(
                    kind="template_status",
                    event_id="2",
                    waba_id="W",
                    template_name="n",
                    template_status="no-such-state",
                ),
            )
            is False
        )
        # an unknown template is recorded
        assert reg.apply_event(
            db,
            WebhookEvent(
                kind="template_status",
                event_id="3",
                waba_id="W",
                template_name="hand_made",
                template_id="77",
                template_status="APPROVED",
            ),
        )
        assert (
            reg.apply_event(
                db,
                WebhookEvent(
                    kind="template_category", event_id="4", waba_id="W", template_id="77", category="bogus"
                ),
            )
            is False
        )
        assert (
            reg.apply_event(
                db,
                WebhookEvent(
                    kind="template_category",
                    event_id="5",
                    waba_id="W",
                    template_id="404",
                    template_name="missing",
                    category="marketing",
                ),
            )
            is False
        )
        db.commit()
    with storage.session() as db:
        # matched by provider id this time
        assert reg.apply_event(
            db,
            WebhookEvent(
                kind="template_status", event_id="6", waba_id="W", template_id="77", template_status="PAUSED"
            ),
        )
        db.commit()
    assert reg.list_templates("W")[0]["status"] == "paused"


# -- webhook handler --------------------------------------------------------------------------


def test_webhook_handler_edges(storage: Storage) -> None:
    class Exploding(MockProvider):
        def parse_webhook(self, payload: dict[str, Any]) -> list[WebhookEvent]:
            if payload.get("explode"):
                raise ValueError("unparseable")
            return [
                WebhookEvent(kind="inbound", event_id="bad", phone_number_id="PN1", phone="1")
            ] + super().parse_webhook(payload)

    engine = Engine(storage, Exploding(), Settings(app_secret="s", verify_token=""))
    wh = engine.webhook
    assert wh.verify({"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x"}) == (
        403,
        "forbidden",
    )

    def post(raw: bytes) -> tuple[int, dict[str, Any]]:
        return wh.receive(raw, sign("s", raw))

    assert post(b"[1, 2]") == (200, {"processed": 0})
    assert post(b'{"explode": true}') == (200, {"processed": 0})

    # an event that fails does not stop the others
    original = engine.process_event

    def flaky(event: WebhookEvent) -> bool:
        if event.event_id == "bad":
            raise RuntimeError("db hiccup")
        return original(event)

    engine.process_event = flaky  # type: ignore[method-assign]
    import json

    from greenline import testing

    raw = json.dumps(testing.inbound_payload(phone_number_id="PN1", phone="15550101", text="hi")).encode()
    assert post(raw) == (200, {"processed": 1})
    assert post(b"") == (200, {"processed": 0})
