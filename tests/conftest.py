from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from greenline import Engine, MockProvider, Settings, Storage
from greenline.storage import utcnow


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real HTTP request fails the test. httpx.MockTransport and TestClient are unaffected."""

    def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"real network call attempted: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def storage() -> Storage:
    s = Storage("sqlite://")
    s.create_all()
    return s


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def make_engine(storage: Storage, provider: MockProvider, clock: Clock) -> Callable[..., Engine]:
    def build(**settings: Any) -> Engine:
        kwargs = {
            k: settings.pop(k)
            for k in ("rate_card", "test_recipients_for", "tenant_resolver")
            if k in settings
        }
        engine = Engine(
            storage,
            provider,
            Settings(provider="mock", **settings),
            clock=clock,
            sleep=lambda _s: None,
            **kwargs,
        )
        return engine

    return build


@pytest.fixture
def engine(make_engine: Callable[..., Engine]) -> Engine:
    e = make_engine()
    e.add_number(
        tenant_id="t1", phone_number_id="PN1", waba_id="W1", token="tok-1", display_phone="+1 555 0100"
    )
    e.templates.add_approved("W1", "notice", category="utility", param_count=2)
    e.templates.add_approved("W1", "promo", category="marketing", param_count=1)
    return e
