"""Engine settings.

Build ``Settings`` directly, or read them from the environment with
``Settings.from_env`` (variables are ``GREENLINE_*`` by default).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any

DEFAULT_STOP_KEYWORDS: tuple[str, ...] = (
    "stop",
    "stop all",
    "stopall",
    "unsubscribe",
    "cancel",
    "end",
    "quit",
    "opt out",
    "optout",
)
DEFAULT_START_KEYWORDS: tuple[str, ...] = ("start", "unstop", "subscribe", "opt in", "optin", "resume")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _optional_bool(value: str) -> bool | None:
    v = value.strip().lower()
    if not v:
        return None
    return v in ("1", "true", "yes", "on")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Everything the engine, providers and webhook need to know."""

    provider: str = "mock"
    """``mock`` or ``meta_cloud`` (or the name of a provider you register yourself)."""

    graph_base_url: str = "https://graph.facebook.com"
    graph_version: str = "v24.0"
    http_timeout: float = 15.0

    app_secret: str = ""
    """Meta app secret, used to verify ``X-Hub-Signature-256`` on webhook POSTs."""
    verify_token: str = ""
    """The token you typed into the Meta webhook settings, echoed back on the GET handshake."""
    fallback_token: str = ""
    """A system-user token used when a number has no stored token, or its own token has expired."""
    token_key: str = ""
    """Fernet key used to encrypt stored access tokens."""
    require_token_key: bool = False

    webhook_path: str = "/webhooks/whatsapp"
    default_language: str = "en"

    sending_enabled: bool | None = None
    """Master switch. ``None`` (unset) means: on for the mock provider, off for real providers."""
    test_mode: bool = False
    """When on, only ``test_recipients`` (and per-tenant test numbers) receive messages."""
    test_recipients: tuple[str, ...] = ()

    max_retries: int = 3
    retry_budget_seconds: float = 5.0
    first_backoff_seconds: float = 0.5

    allow_marketing_without_opt_in: bool = False
    daily_cap_per_number: int = 0
    """Business-initiated (template) sends per number per rolling 24 h; ``0`` disables the cap."""
    monthly_budget_minor: int = 0
    """Pause marketing sends once this month's metered spend reaches this (minor units); ``0`` is off."""

    inbound_counts_as_opt_in: bool = True
    """Treat a first message from a person as consent to hear back (including marketing)."""
    window_hours: int = 24
    attribution_days: int = 30

    stop_keywords: tuple[str, ...] = DEFAULT_STOP_KEYWORDS
    start_keywords: tuple[str, ...] = DEFAULT_START_KEYWORDS

    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "GREENLINE_", environ: Mapping[str, str] | None = None) -> Settings:
        """Read settings from environment variables named ``<prefix><FIELD_NAME>``.

        Unset variables keep their defaults. Lists are comma separated.
        """
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "extra":
                continue
            raw = env.get(prefix + f.name.upper())
            if raw is None:
                continue
            default = f.default
            if f.name == "sending_enabled":
                values[f.name] = _optional_bool(raw)
            elif isinstance(default, bool):
                values[f.name] = _bool(raw)
            elif isinstance(default, int):
                values[f.name] = int(raw)
            elif isinstance(default, float):
                values[f.name] = float(raw)
            elif isinstance(default, tuple):
                values[f.name] = tuple(v.lower() for v in _csv(raw)) if "keywords" in f.name else _csv(raw)
            else:
                values[f.name] = raw
        return cls(**values)

    def with_changes(self, **changes: Any) -> Settings:
        """A copy with some fields replaced."""
        return replace(self, **changes)

    @property
    def sending_on(self) -> bool:
        """Whether the master switch allows sending at all."""
        if self.sending_enabled is None:
            return self.provider == "mock"
        return self.sending_enabled
