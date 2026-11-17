"""The engine: sending with every safeguard, and processing what the webhook delivers.

Outbound, in order:

1. Master switch and test mode (:mod:`greenline.gate`).
2. Idempotency: a repeated ``idempotency_key`` returns the first result without sending again.
3. Consent and the customer-service window: opted-out contacts get nothing; free-form text only
   goes inside the window; marketing templates need an opt-in.
4. Number protection: an optional per-number daily cap on business-initiated messages, marketing
   held while Meta rates the number red, and an optional monthly marketing budget.
5. The message row is written (reserving the idempotency key) *before* the provider is called,
   so two concurrent requests with one key cannot both send.
6. The provider call, retried with exponential backoff for retryable errors. Retries sleep in the
   caller's thread, so their total wait is capped (``retry_budget_seconds``).
7. The result and an estimated cost are stored on the row.

Inbound: webhook items are de-duplicated by id, applied in one transaction (messages, receipts,
STOP/START, template review results, number quality), and hooks fire after the commit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from greenline import consent
from greenline.billing import FREE, RateCard
from greenline.config import Settings
from greenline.errors import NumberNotReady, ProviderError, RateLimited
from greenline.gate import SendGate, TenantRecipients, digits
from greenline.hooks import Hooks
from greenline.params import clean_param, fit_params
from greenline.providers import Provider, build_provider
from greenline.providers.meta_cloud import TOKEN_EXPIRED_CODE
from greenline.schemas import (
    Category,
    Outbound,
    ProviderResult,
    Sender,
    SendResult,
    TemplateMessage,
    WebhookEvent,
)
from greenline.security import TokenCipher
from greenline.storage import Storage, utcnow
from greenline.templates import TemplateDefinition, TemplateRegistry

log = logging.getLogger("greenline")

# Delivery states only move forward. ``failed`` may replace queued or sent only: a message the
# provider already delivered cannot become undelivered.
STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
_FAILABLE = frozenset({"queued", "sent"})

# Skip reasons meaning "do not message this person now"; callers should not try another channel
# of the same kind to get around them.
POLICY_SKIPS = frozenset(
    {"opted_out", "not_opted_in", "duplicate", "daily_cap", "test_mode", "sending_paused"}
)

_QUALITY = {"flagged": "red", "unflagged": "green"}

TenantResolver = Callable[[WebhookEvent], "str | None"]


def status_advances(current: str, new: str) -> bool:
    """True when a delivery receipt moves a message forward."""
    if new == "failed":
        return current in _FAILABLE
    if new not in STATUS_ORDER or current not in STATUS_ORDER:
        return False
    return STATUS_ORDER[new] > STATUS_ORDER[current]


class Engine:
    """A multi-tenant messaging engine.

    Args:
        storage: where messages, contacts, numbers and templates are kept.
        provider: the messaging backend; built from ``settings.provider`` when omitted.
        settings: engine settings (defaults: mock provider, no secrets).
        rate_card: prices used to estimate the cost of each message; no metering when omitted.
        test_recipients_for: ``tenant_id -> numbers`` allowed in test mode, besides
            ``settings.test_recipients``.
        tenant_resolver: called for an inbound message on a shared number before the
            "last tenant who messaged them" rule; return a tenant id to claim it.
        clock: returns the current naive-UTC time (tests move it forward).
        sleep: used between retries (tests pass a no-op).
    """

    def __init__(
        self,
        storage: Storage,
        provider: Provider | None = None,
        settings: Settings | None = None,
        *,
        rate_card: RateCard | None = None,
        test_recipients_for: TenantRecipients | None = None,
        tenant_resolver: TenantResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or Settings()
        self.storage = storage
        self.provider: Provider = provider or build_provider(self.settings)
        self.rate_card = rate_card or FREE
        self.hooks = Hooks()
        self.templates = TemplateRegistry(storage, self.settings.default_language)
        self.gate = SendGate(self.settings, test_recipients_for)
        self.cipher = TokenCipher(self.settings.token_key, required=self.settings.require_token_key)
        self.tenant_resolver = tenant_resolver
        self.clock: Callable[[], datetime] = clock or utcnow
        self._sleep = sleep
        self.m = storage.models

        from greenline.webhook import WebhookHandler

        self.webhook = WebhookHandler(self)

    # == numbers ================================================================================

    def add_number(
        self,
        *,
        phone_number_id: str,
        token: str = "",
        tenant_id: str = "",
        waba_id: str = "",
        display_phone: str = "",
        display_name: str = "",
    ) -> str:
        """Register (or update) a sending number. Returns its row id.

        A number with a ``tenant_id`` sends for that tenant only. A number without one is shared:
        every tenant that has no number of its own sends from it. The token is encrypted at rest.
        """
        N = self.m.Number
        with self.storage.session() as db:
            row = db.scalar(select(N).where(N.phone_number_id == phone_number_id))
            if row is None:
                row = N(phone_number_id=phone_number_id)
                db.add(row)
            row.tenant_id = tenant_id
            row.waba_id = waba_id
            row.display_phone = display_phone
            row.display_name = display_name
            row.status = "live"
            if token:
                row.token_enc = self.cipher.encrypt(token)
            db.commit()
            return str(row.id)

    def disable_number(self, phone_number_id: str) -> None:
        N = self.m.Number
        with self.storage.session() as db:
            row = db.scalar(select(N).where(N.phone_number_id == phone_number_id))
            if row is not None:
                row.status = "disabled"
                db.commit()

    def numbers(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Registered numbers (tokens left out), optionally only one tenant's own."""
        N = self.m.Number
        q = select(N).order_by(N.created_at)
        if tenant_id is not None:
            q = q.where(N.tenant_id == tenant_id)
        with self.storage.session() as db:
            return [
                {
                    "phone_number_id": n.phone_number_id,
                    "tenant_id": n.tenant_id,
                    "waba_id": n.waba_id,
                    "display_phone": n.display_phone,
                    "display_name": n.display_name,
                    "status": n.status,
                    "quality": n.quality,
                    "tier": n.tier,
                    "shared": not n.tenant_id,
                }
                for n in db.scalars(q)
            ]

    def sender_for(self, tenant_id: str) -> Sender:
        """The tenant's own newest live number, else the oldest live shared number.

        Raises :class:`NumberNotReady` when neither exists.
        """
        N = self.m.Number
        with self.storage.session() as db:
            row = None
            if tenant_id:
                row = db.scalar(
                    select(N)
                    .where(N.tenant_id == tenant_id, N.status == "live")
                    .order_by(N.created_at.desc())
                    .limit(1)
                )
            if row is None:
                row = db.scalar(
                    select(N).where(N.tenant_id == "", N.status == "live").order_by(N.created_at).limit(1)
                )
            if row is None:
                raise NumberNotReady(f"no live number for tenant {tenant_id!r} and no shared number")
            token = self.cipher.decrypt(row.token_enc) or self.settings.fallback_token
            return Sender(
                phone_number_id=row.phone_number_id,
                token=token,
                waba_id=row.waba_id,
                display_phone=row.display_phone,
                display_name=row.display_name,
            )

    # == consent ================================================================================

    def record_opt_in(
        self, tenant_id: str, phone: str, *, source: str = "", external_id: str | None = None
    ) -> bool:
        """Record that this person agreed to hear from the tenant.

        Returns False (and changes nothing) if they have opted out: only their own START
        message can undo a STOP.
        """
        sender = self.sender_for(tenant_id)
        with self.storage.session() as db:
            c = self._get_or_create_contact(db, tenant_id, sender.phone_number_id, digits(phone), external_id)
            if c.opted_out:
                db.commit()
                return False
            c.opted_in = True
            c.opt_in_source = source
            c.updated_at = self.clock()
            db.commit()
            return True

    def contact(self, tenant_id: str, phone: str) -> dict[str, Any] | None:
        """Consent and window state for this person on the tenant's sending number."""
        sender = self.sender_for(tenant_id)
        with self.storage.session() as db:
            c = self._find_contact(db, sender.phone_number_id, digits(phone))
            if c is None:
                return None
            return {
                "phone": c.phone,
                "phone_number_id": c.phone_number_id,
                "opted_in": c.opted_in,
                "opted_out": c.opted_out,
                "opt_in_source": c.opt_in_source,
                "in_window": consent.in_window(c, self.clock()),
                "window_expires_at": c.window_expires_at,
                "last_inbound_at": c.last_inbound_at,
            }

    # == sending ================================================================================

    def send(
        self,
        *,
        tenant_id: str,
        to: str,
        text: str = "",
        template: str | TemplateMessage | None = None,
        params: Iterable[Any] | None = None,
        language: str | None = None,
        idempotency_key: str | None = None,
        external_id: str | None = None,
    ) -> SendResult:
        """Send the right kind of message for where the conversation stands.

        Inside the customer-service window, ``text`` goes out as a free-form message. Outside it
        (or without text), ``template`` is sent instead: either the name of a template approved
        on the sending account, or a :class:`TemplateMessage` you built yourself. ``params`` fill
        the template's variables (``[text]`` when omitted), sanitised for Meta and padded or
        truncated to the template's variable count.

        Never raises for policy reasons: a refused send has ``ok=False`` and ``skipped`` set.
        Raises :class:`NumberNotReady` when the tenant has no number to send from.
        """
        phone = digits(to)
        if not phone:
            return SendResult(ok=False, skipped="no_phone")
        text = (text or "").strip()
        if not text and template is None:
            return SendResult(ok=False, skipped="empty_message")
        sender = self.sender_for(tenant_id)

        with self.storage.session() as db:
            c = self._find_contact(db, sender.phone_number_id, phone)
            opted_out = bool(c is not None and c.opted_out)
            window_open = consent.in_window(c, self.clock())
        if opted_out:
            return SendResult(ok=False, skipped="opted_out")

        if window_open and text:
            out = Outbound(kind="text", to=phone, category="service", text=text)
            return self._dispatch(tenant_id, sender, out, idempotency_key, external_id)

        if template is None:
            return SendResult(ok=False, skipped="outside_window")

        values = list(params) if params is not None else [text]
        if isinstance(template, str):
            resolved = self.templates.resolve(sender.waba_id, template, language)
            if resolved is None:
                return SendResult(ok=False, skipped=f"no_approved_template:{template}")
            tm = TemplateMessage(
                name=resolved.name,
                language=resolved.language,
                category=cast(Category, resolved.category),
                params=values,
                param_count=resolved.param_count,
            )
        else:
            tm = template
            if params is not None:
                tm.params = values
        return self.send_template(
            tenant_id=tenant_id,
            to=phone,
            template=tm,
            idempotency_key=idempotency_key,
            external_id=external_id,
        )

    def send_text(
        self,
        *,
        tenant_id: str,
        to: str,
        text: str,
        idempotency_key: str | None = None,
        external_id: str | None = None,
    ) -> SendResult:
        """Send free-form text. Refused (``outside_window``) unless the window is open."""
        phone = digits(to)
        if not phone:
            return SendResult(ok=False, skipped="no_phone")
        if not (text or "").strip():
            return SendResult(ok=False, skipped="empty_message")
        out = Outbound(kind="text", to=phone, category="service", text=text.strip())
        return self._dispatch(tenant_id, self.sender_for(tenant_id), out, idempotency_key, external_id)

    def send_template(
        self,
        *,
        tenant_id: str,
        to: str,
        template: TemplateMessage,
        idempotency_key: str | None = None,
        external_id: str | None = None,
    ) -> SendResult:
        """Send a template. Its params are sanitised (and fitted when ``param_count`` is set)."""
        phone = digits(to)
        if not phone:
            return SendResult(ok=False, skipped="no_phone")
        if template.param_count is not None:
            template.params = fit_params(template.params, template.param_count)
        else:
            template.params = [clean_param(p) for p in template.params]
        out = Outbound(kind="template", to=phone, category=template.category, template=template)
        return self._dispatch(tenant_id, self.sender_for(tenant_id), out, idempotency_key, external_id)

    def _dispatch(
        self,
        tenant_id: str,
        sender: Sender,
        out: Outbound,
        idempotency_key: str | None,
        external_id: str | None,
    ) -> SendResult:
        blocked = self.gate.check(out.to, tenant_id)
        if blocked:
            return SendResult(ok=False, skipped=blocked)
        M = self.m.Message
        with self.storage.session() as db:
            if idempotency_key:
                prior = db.scalar(select(M).where(M.idempotency_key == idempotency_key))
                if prior is not None:
                    return _duplicate(prior)

            c = self._get_or_create_contact(db, tenant_id, sender.phone_number_id, out.to, external_id)
            now = self.clock()
            window_open = consent.in_window(c, now)
            skipped = self._skip_reason(db, sender, c, out, window_open, now)
            if skipped:
                db.commit()  # keep the contact row
                return SendResult(ok=False, skipped=skipped)

            msg = M(
                tenant_id=tenant_id,
                phone_number_id=sender.phone_number_id,
                contact_id=c.id,
                direction="out",
                kind=out.kind,
                category=out.category,
                template_name=out.template.name if out.template else "",
                body=out.text or _template_summary(out.template),
                status="sending",
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
            db.add(msg)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                prior = db.scalar(select(M).where(M.idempotency_key == idempotency_key))
                return _duplicate(prior) if prior is not None else SendResult(ok=False, skipped="duplicate")

            result = self._send_with_retry(sender, out)
            result = self._retry_with_fallback_token(sender, out, result)
            self._record_result(db, sender, out, msg, result, window_open)
            db.commit()
            return SendResult(
                ok=result.ok,
                message_id=msg.id,
                provider_message_id=msg.provider_message_id,
                status=msg.status,
                kind=out.kind,
                template_name=msg.template_name,
                error=result.error,
                error_code=result.error_code,
            )

    def _skip_reason(
        self, db: Session, sender: Sender, c: Any, out: Outbound, window_open: bool, now: datetime
    ) -> str | None:
        blocked = consent.policy_block(
            c,
            out.category,
            window_open=window_open,
            allow_marketing_without_opt_in=self.settings.allow_marketing_without_opt_in,
        )
        if blocked:
            return blocked
        if out.kind == "text" and not window_open:
            return "outside_window"
        M, N = self.m.Message, self.m.Number
        if out.category == "marketing":
            quality = db.scalar(select(N.quality).where(N.phone_number_id == sender.phone_number_id))
            if quality == "red":
                log.warning("marketing held: number %s is rated red", sender.phone_number_id)
                return "quality_paused"
            budget = self.settings.monthly_budget_minor
            if budget and self._spent(db, _month_start(now)) >= budget:
                return "budget_exhausted"
        cap = self.settings.daily_cap_per_number
        if cap and out.category != "service" and not window_open:
            sent = db.scalar(
                select(func.count())
                .select_from(M)
                .where(
                    M.phone_number_id == sender.phone_number_id,
                    M.direction == "out",
                    M.category != "service",
                    M.created_at >= now - timedelta(hours=24),
                )
            )
            if (sent or 0) >= cap:
                return "daily_cap"
        return None

    def _send_with_retry(self, sender: Sender, out: Outbound) -> ProviderResult:
        s = self.settings
        attempt, budget = 0, s.retry_budget_seconds
        last: ProviderError | None = None
        while attempt <= s.max_retries:
            try:
                return self.provider.send(sender, out)
            except RateLimited as exc:
                last = exc
                wait = exc.retry_after if exc.retry_after else s.first_backoff_seconds * 2**attempt
            except ProviderError as exc:
                last = exc
                if not exc.retryable:
                    return ProviderResult(ok=False, status="failed", error_code=exc.code, error=str(exc))
                wait = s.first_backoff_seconds * 2**attempt
            attempt += 1
            if attempt > s.max_retries or wait > budget:
                break
            self._sleep(wait)
            budget -= wait
        return ProviderResult(
            ok=False,
            status="failed",
            error_code=last.code if last else "exhausted",
            error=str(last) if last else "retries exhausted",
        )

    def _retry_with_fallback_token(
        self, sender: Sender, out: Outbound, result: ProviderResult
    ) -> ProviderResult:
        """A tenant's own token can expire. Try once more with the fallback (system) token."""
        if result.ok or result.error_code != TOKEN_EXPIRED_CODE:
            return result
        fallback = self.settings.fallback_token
        if fallback and sender.token != fallback:
            log.warning("token for %s expired; retrying with the fallback token", sender.phone_number_id)
            retry_sender = Sender(
                phone_number_id=sender.phone_number_id,
                token=fallback,
                waba_id=sender.waba_id,
                display_phone=sender.display_phone,
                display_name=sender.display_name,
            )
            result = self._send_with_retry(retry_sender, out)
        if not result.ok and result.error_code == TOKEN_EXPIRED_CODE:
            result.error = "the access token for this number has expired; reconnect the number"
        return result

    def _record_result(
        self, db: Session, sender: Sender, out: Outbound, msg: Any, result: ProviderResult, window_open: bool
    ) -> None:
        msg.provider_message_id = result.provider_message_id or ""
        msg.status = result.status if result.ok else "failed"
        msg.error_code, msg.error = result.error_code or "", result.error or ""
        msg.updated_at = self.clock()
        if not result.ok:
            msg.cost_minor = 0
            # A failed attempt frees its key so a retry can send. When delivery is unknown the
            # message may have gone out, so the key stays reserved to prevent a double send.
            if result.error_code != "network_unknown":
                msg.idempotency_key = None
            return
        served = self._service_this_month(db, sender, msg.id) if out.category == "service" else 0
        msg.cost_minor = self.rate_card.cost(
            out.category, in_window=window_open, service_sent_this_month=served
        )

    def _service_this_month(self, db: Session, sender: Sender, exclude_id: str) -> int:
        M = self.m.Message
        return int(
            db.scalar(
                select(func.count())
                .select_from(M)
                .where(
                    M.phone_number_id == sender.phone_number_id,
                    M.direction == "out",
                    M.category == "service",
                    M.status != "failed",
                    M.created_at >= _month_start(self.clock()),
                    M.id != exclude_id,
                )
            )
            or 0
        )

    def _find_contact(self, db: Session, phone_number_id: str, phone: str) -> Any:
        C = self.m.Contact
        return db.scalar(select(C).where(C.phone_number_id == phone_number_id, C.phone == phone))

    def _get_or_create_contact(
        self, db: Session, tenant_id: str, phone_number_id: str, phone: str, external_id: str | None
    ) -> Any:
        c = self._find_contact(db, phone_number_id, phone)
        if c is None:
            now = self.clock()
            c = self.m.Contact(
                tenant_id=tenant_id,
                phone_number_id=phone_number_id,
                phone=phone,
                external_id=external_id,
                created_at=now,
                updated_at=now,
            )
            db.add(c)
            db.flush()
        elif external_id and not c.external_id and c.tenant_id == tenant_id:
            c.external_id = external_id
        return c

    # == inbound ================================================================================

    def process_webhook(self, payload: dict[str, Any]) -> int:
        """Parse an already-verified webhook body and process every item. Returns how many were new."""
        return self.process_events(self.provider.parse_webhook(payload))

    def process_events(self, events: Iterable[WebhookEvent]) -> int:
        """Process parsed events (a replay, a simulator). Returns how many were new."""
        return sum(1 for e in events if self.process_event(e))

    def process_event(self, event: WebhookEvent) -> bool:
        """Apply one event. Idempotent: returns False for an id already processed.

        Hooks fire after the transaction commits.
        """
        W = self.m.WebhookEvent
        emits: list[tuple[str, dict[str, Any]]] = []
        with self.storage.session() as db:
            if db.scalar(select(W).where(W.event_id == event.event_id)) is not None:
                return False
            db.add(W(event_id=event.event_id, kind=event.kind, received_at=self.clock()))
            try:
                db.flush()
            except IntegrityError:  # pragma: no cover - a concurrent worker took it
                db.rollback()
                return False
            if event.kind == "inbound":
                self._handle_inbound(db, event, emits)
            elif event.kind == "status":
                self._handle_status(db, event, emits)
            elif event.kind in ("template_status", "template_category"):
                self._handle_template(db, event, emits)
            elif event.kind == "quality":
                self._handle_quality(db, event, emits)
            db.commit()
        for name, payload in emits:
            self.hooks.emit(name, **payload)
        return True

    def attribute(self, db: Session, event: WebhookEvent, contact: Any) -> str:
        """Which tenant an inbound message belongs to ("" when nobody can be found).

        A tenant's own number names its tenant. On a shared number, ``tenant_resolver`` gets the
        first say; otherwise the message goes to the tenant that last messaged this person from
        that number within ``attribution_days``.
        """
        N, M = self.m.Number, self.m.Message
        owner = db.scalar(select(N.tenant_id).where(N.phone_number_id == event.phone_number_id))
        if owner:
            return str(owner)
        if self.tenant_resolver is not None:
            try:
                claimed = self.tenant_resolver(event)
            except Exception:
                log.exception("tenant_resolver failed")
                claimed = None
            if claimed:
                return claimed
        if contact is None:
            return ""
        since = self.clock() - timedelta(days=self.settings.attribution_days)
        last = db.scalar(
            select(M.tenant_id)
            .where(
                M.phone_number_id == event.phone_number_id,
                M.contact_id == contact.id,
                M.direction == "out",
                M.created_at >= since,
            )
            .order_by(M.created_at.desc())
            .limit(1)
        )
        return str(last or "")

    def _handle_inbound(
        self, db: Session, event: WebhookEvent, emits: list[tuple[str, dict[str, Any]]]
    ) -> None:
        now = self.clock()
        phone = digits(event.phone)
        c = self._find_contact(db, event.phone_number_id, phone)
        tenant_id = self.attribute(db, event, c)
        if c is None:
            c = self.m.Contact(
                tenant_id=tenant_id,
                phone_number_id=event.phone_number_id,
                phone=phone,
                opted_in=self.settings.inbound_counts_as_opt_in,
                opt_in_source="inbound" if self.settings.inbound_counts_as_opt_in else "",
                created_at=now,
            )
            db.add(c)
            db.flush()
        c.last_inbound_at = now
        c.window_expires_at = consent.window_end(now, self.settings.window_hours)
        c.updated_at = now
        # On a shared number one contact row serves every tenant, so its external id only means
        # something to the tenant that created it.
        external_id = c.external_id if c.tenant_id == tenant_id else None

        action = consent.keyword_action(event.text, self.settings.stop_keywords, self.settings.start_keywords)
        if event.button_payload.split(":", 1)[0].lower() == "stop":  # a "Stop promotions" quick reply
            action = "stop"
        common = {
            "tenant_id": tenant_id,
            "phone": phone,
            "phone_number_id": event.phone_number_id,
            "external_id": external_id,
        }
        if action == "stop":
            c.opted_out = True
            emits.append(("opt_out", dict(common)))
        elif action == "start":
            c.opted_out = False
            c.opted_in = True
            c.opt_in_source = "keyword"
            emits.append(("opt_in", dict(common)))

        msg = self.m.Message(
            tenant_id=tenant_id,
            phone_number_id=event.phone_number_id,
            contact_id=c.id,
            direction="in",
            kind=event.message_type or "text",
            category="",
            body=event.text,
            provider_message_id=event.provider_message_id,
            status="received",
            created_at=now,
            updated_at=now,
        )
        db.add(msg)
        db.flush()
        emits.append(
            (
                "inbound",
                {
                    **common,
                    "text": event.text,
                    "message_type": event.message_type,
                    "button_payload": event.button_payload,
                    "provider_message_id": event.provider_message_id,
                    "message_id": msg.id,
                },
            )
        )

    def _handle_status(
        self, db: Session, event: WebhookEvent, emits: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Apply a receipt, never moving a message backwards (``delivered`` can arrive after
        ``read``). The hook fires for an applied change, or for a message we did not send."""
        M, C = self.m.Message, self.m.Contact
        new = (event.status or "").lower()
        msg = db.scalar(
            select(M).where(M.provider_message_id == event.provider_message_id, M.direction == "out").limit(1)
        )
        tenant_id, phone, message_id = "", digits(event.phone), ""
        if msg is not None:
            if not status_advances(msg.status, new):
                return
            msg.status = new
            if event.error_code:
                msg.error_code = event.error_code
            if new == "failed":
                msg.cost_minor = 0  # only delivered messages are billed
            msg.updated_at = self.clock()
            tenant_id, message_id = msg.tenant_id, msg.id
            contact = db.get(C, msg.contact_id) if msg.contact_id else None
            if contact is not None:
                phone = contact.phone
        emits.append(
            (
                "status",
                {
                    "tenant_id": tenant_id,
                    "phone": phone,
                    "status": new,
                    "error_code": event.error_code,
                    "provider_message_id": event.provider_message_id,
                    "message_id": message_id,
                },
            )
        )

    def _handle_template(
        self, db: Session, event: WebhookEvent, emits: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self.templates.apply_event(db, event)
        base = {"waba_id": event.waba_id, "name": event.template_name, "language": event.template_language}
        if event.kind == "template_category":
            emits.append(("template_category", {**base, "category": event.category}))
        else:
            emits.append(
                ("template_status", {**base, "status": event.template_status, "reason": event.reason})
            )

    def _handle_quality(
        self, db: Session, event: WebhookEvent, emits: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """``phone_number_quality_update`` names the number by its display phone."""
        N = self.m.Number
        rating = _QUALITY.get(event.quality)
        phone = digits(event.phone)
        if not phone:
            return
        q = select(N)
        if event.waba_id:
            q = q.where(N.waba_id == event.waba_id)
        for num in db.scalars(q):
            if digits(num.display_phone) != phone:
                continue
            if rating:
                if rating == "red" and num.quality != "red":
                    log.warning("number %s rated red: marketing sends are held", num.display_phone)
                num.quality = rating
            if event.tier:
                num.tier = event.tier
        emits.append(
            (
                "quality",
                {
                    "waba_id": event.waba_id,
                    "display_phone": event.phone,
                    "quality": event.quality,
                    "tier": event.tier,
                },
            )
        )

    # == templates ==============================================================================

    def _account_token(self, waba_id: str) -> str:
        N = self.m.Number
        with self.storage.session() as db:
            for enc in db.scalars(select(N.token_enc).where(N.waba_id == waba_id, N.token_enc != "")):
                return self.cipher.decrypt(enc)
        return self.settings.fallback_token

    def submit_templates(
        self, waba_id: str, definitions: Iterable[TemplateDefinition], *, token: str | None = None
    ) -> list[dict[str, Any]]:
        """Submit template definitions to an account (idempotent). See :meth:`TemplateRegistry.submit`."""
        return self.templates.submit(
            self.provider, waba_id, token or self._account_token(waba_id), definitions
        )

    def sync_templates(self, waba_id: str, *, token: str | None = None) -> list[dict[str, str]]:
        """Refresh the local copy of an account's templates and their review states."""
        return self.templates.sync(self.provider, waba_id, token or self._account_token(waba_id))

    # == metering ===============================================================================

    def _spent(self, db: Session, since: datetime) -> int:
        M = self.m.Message
        total = db.scalar(
            select(func.coalesce(func.sum(M.cost_minor), 0)).where(
                M.direction == "out", M.status != "failed", M.created_at >= since
            )
        )
        return int(total or 0)

    def usage(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> dict[str, dict[str, int]]:
        """Messages sent and estimated cost per tenant: ``{tenant_id: {"messages", "cost_minor"}}``.

        ``since`` defaults to the start of the current calendar month; ``until`` (exclusive) to no
        limit. Failed messages are not counted.
        """
        M = self.m.Message
        cond = [
            M.direction == "out",
            M.status != "failed",
            M.created_at >= (since or _month_start(self.clock())),
        ]
        if until is not None:
            cond.append(M.created_at < until)
        with self.storage.session() as db:
            rows = db.execute(
                select(M.tenant_id, func.count(), func.coalesce(func.sum(M.cost_minor), 0))
                .where(*cond)
                .group_by(M.tenant_id)
            ).all()
        return {str(t): {"messages": int(n), "cost_minor": int(c)} for t, n, c in rows}

    def message(self, message_id: str) -> dict[str, Any] | None:
        """One stored message as a dict, or None."""
        with self.storage.session() as db:
            m = db.get(self.m.Message, message_id)
            if m is None:
                return None
            return {
                "id": m.id,
                "tenant_id": m.tenant_id,
                "phone_number_id": m.phone_number_id,
                "direction": m.direction,
                "kind": m.kind,
                "category": m.category,
                "template_name": m.template_name,
                "body": m.body,
                "provider_message_id": m.provider_message_id,
                "status": m.status,
                "error_code": m.error_code,
                "error": m.error,
                "cost_minor": m.cost_minor,
                "created_at": m.created_at,
            }


def _duplicate(prior: Any) -> SendResult:
    return SendResult(
        ok=prior.status != "failed",
        message_id=prior.id,
        provider_message_id=prior.provider_message_id,
        status=prior.status,
        kind=prior.kind,
        template_name=prior.template_name,
        skipped="duplicate",
    )


def _template_summary(t: TemplateMessage | None) -> str:
    """What a template send said, for the audit trail: its variables, else its name."""
    if t is None:
        return ""
    return "\n".join(p for p in t.params if p and p != "-") or t.name


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
