"""Consent rules: STOP/START keywords, the customer-service window, and the send policy."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal, Protocol

_NON_WORD = re.compile(r"[^a-z0-9]+")


class ContactState(Protocol):
    opted_in: bool
    opted_out: bool
    window_expires_at: datetime | None


def normalise(text: str) -> str:
    """Lowercase, drop punctuation and emoji, collapse whitespace: ``"  Stop! "`` -> ``"stop"``."""
    return " ".join(_NON_WORD.sub(" ", (text or "").lower()).split())


def keyword_action(
    text: str, stop_keywords: Iterable[str], start_keywords: Iterable[str]
) -> Literal["stop", "start"] | None:
    """``"stop"`` or ``"start"`` when the whole message is one of the keywords, else ``None``.

    Only whole-message matches count, so "nonstop" or "restart the order" do nothing.
    """
    t = normalise(text)
    if not t:
        return None
    if t in {normalise(k) for k in stop_keywords if k.strip()}:
        return "stop"
    if t in {normalise(k) for k in start_keywords if k.strip()}:
        return "start"
    return None


def in_window(contact: ContactState | None, now: datetime) -> bool:
    """Whether the contact's customer-service window is open."""
    return bool(contact is not None and contact.window_expires_at and contact.window_expires_at > now)


def window_end(now: datetime, hours: int) -> datetime:
    return now + timedelta(hours=hours)


def policy_block(
    contact: ContactState, category: str, *, window_open: bool, allow_marketing_without_opt_in: bool
) -> str | None:
    """Why this contact may not receive a message of ``category``, or ``None`` when allowed.

    Opted-out contacts get nothing. Inside the window anything goes. Outside it, utility and
    authentication templates are allowed, and marketing needs an opt-in.
    """
    if contact.opted_out:
        return "opted_out"
    if category == "service" or window_open:
        return None
    if category == "marketing" and not (contact.opted_in or allow_marketing_without_opt_in):
        return "not_opted_in"
    return None
