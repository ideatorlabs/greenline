"""The master switch and test mode.

Two safety nets that sit in front of every send:

* **Master switch** (``Settings.sending_enabled``). Off means nothing leaves. Left unset, it is
  on for the mock provider and off for any real provider, so a missing configuration line never
  results in real messages.
* **Test mode** (``Settings.test_mode``). Only allow-listed recipients get real messages; every
  other send is skipped with ``skipped="test_mode"``. The allow list is
  ``Settings.test_recipients`` plus whatever the optional per-tenant callback returns.

Numbers are compared on their last ten digits, so ``+1 555 0100`` style formatting and a missing
country code still match.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from greenline.config import Settings

MATCH_DIGITS = 10

TenantRecipients = Callable[[str], Iterable[str]]


def digits(phone: str | None) -> str:
    """Only the digits of a phone number (Meta's wire format: E.164 without ``+``)."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def tail(phone: str | None, n: int = MATCH_DIGITS) -> str:
    return digits(phone)[-n:]


class SendGate:
    def __init__(self, settings: Settings, tenant_recipients: TenantRecipients | None = None) -> None:
        self.settings = settings
        self.tenant_recipients = tenant_recipients

    @property
    def enabled(self) -> bool:
        return self.settings.sending_on

    @property
    def test_mode(self) -> bool:
        return self.settings.test_mode

    def allowed_numbers(self, tenant_id: str | None) -> set[str]:
        numbers = set(self.settings.test_recipients)
        if tenant_id and self.tenant_recipients is not None:
            numbers |= set(self.tenant_recipients(tenant_id) or ())
        return {t for t in (tail(n) for n in numbers) if t}

    def recipient_allowed(self, phone: str | None, tenant_id: str | None = None) -> bool:
        """False only in test mode, for a number that is not on the allow list."""
        if not self.test_mode:
            return True
        t = tail(phone)
        return bool(t) and t in self.allowed_numbers(tenant_id)

    def check(self, phone: str | None, tenant_id: str | None = None) -> str | None:
        """The skip reason for this recipient, or ``None`` when the send may go ahead."""
        if not self.enabled:
            return "sending_paused"
        if not self.recipient_allowed(phone, tenant_id):
            return "test_mode"
        return None
