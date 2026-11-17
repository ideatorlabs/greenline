"""Template management: define, submit, sync and resolve approved templates.

A template belongs to one WhatsApp Business Account (WABA), must be approved by Meta before it
can be sent, has a category (which drives both the consent rules and the price) and declares a
fixed number of body variables. The registry keeps a local copy of what each WABA holds so sends
can check approval and variable count without a network call; it is refreshed by
:meth:`TemplateRegistry.sync` and by ``message_template_status_update`` webhooks.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from greenline.params import count_variables
from greenline.providers.base import Provider
from greenline.schemas import WebhookEvent
from greenline.storage import Storage, utcnow

log = logging.getLogger("greenline.templates")

STATUSES = ("approved", "pending", "rejected", "paused", "disabled")

_STATUS_MAP = {
    "approved": "approved",
    "reinstated": "approved",
    "flagged": "approved",  # a quality warning; it still sends until Meta pauses it
    "pending": "pending",
    "in_appeal": "pending",
    "rejected": "rejected",
    "limit_exceeded": "rejected",
    "paused": "paused",
    "disabled": "disabled",
    "pending_deletion": "disabled",
    "deleted": "disabled",
    "archived": "disabled",
}
# A name already on the account in one of these states is never submitted again: Meta refuses
# duplicate names, and an approved template cannot be edited.
_ALREADY_SUBMITTED = frozenset({"approved", "pending", "paused", "disabled"})
_RANK = {"approved": 5, "pending": 4, "paused": 3, "rejected": 2, "disabled": 1}


def normalise_status(value: str) -> str | None:
    """Meta's review state folded into approved | pending | rejected | paused | disabled."""
    return _STATUS_MAP.get((value or "").strip().lower())


@dataclass(frozen=True)
class Button:
    """A template button. ``kind`` is ``quick_reply``, ``url`` or ``copy_code``."""

    kind: str
    text: str = ""
    url: str = ""
    example: str = ""

    def component(self) -> dict[str, Any]:
        if self.kind == "url":
            spec: dict[str, Any] = {"type": "URL", "text": self.text, "url": self.url}
            if self.example:
                spec["example"] = [self.example]
            return spec
        if self.kind == "copy_code":
            return {"type": "COPY_CODE", "example": self.example or "CODE10"}
        return {"type": "QUICK_REPLY", "text": self.text}


@dataclass(frozen=True)
class TemplateDefinition:
    """A template you want to exist on an account.

    Meta rejects a body that starts or ends with a variable, and asks for sample values for every
    variable at review time (``example``; missing samples are filled with ``sample N``).
    """

    name: str
    category: str
    body: str
    language: str = "en"
    example: tuple[str, ...] = ()
    footer: str = ""
    buttons: tuple[Button, ...] = field(default_factory=tuple)

    @property
    def param_count(self) -> int:
        return count_variables(self.body)

    def components(self) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"type": "BODY", "text": self.body}
        n = self.param_count
        if n:
            samples = list(self.example)[:n]
            samples += [f"sample {i + 1}" for i in range(len(samples), n)]
            body["example"] = {"body_text": [samples]}
        out: list[dict[str, Any]] = [body]
        if self.footer:
            out.append({"type": "FOOTER", "text": self.footer})
        if self.buttons:
            out.append({"type": "BUTTONS", "buttons": [b.component() for b in self.buttons]})
        return out


@dataclass(frozen=True)
class ResolvedTemplate:
    name: str
    language: str
    category: str
    param_count: int | None
    """``None`` when the template's body is unknown locally (params are then sent as given)."""
    waba_id: str


class TemplateRegistry:
    def __init__(self, storage: Storage, default_language: str = "en") -> None:
        self.storage = storage
        self.default_language = default_language

    @property
    def _T(self) -> Any:
        return self.storage.models.Template

    # -- reads ------------------------------------------------------------------------------

    def resolve(self, waba_id: str, name: str, language: str | None = None) -> ResolvedTemplate | None:
        """The approved template called ``name`` on ``waba_id``, preferring ``language``; else None."""
        T = self._T
        with self.storage.session() as db:
            rows = list(
                db.scalars(select(T).where(T.waba_id == waba_id, T.name == name, T.status == "approved"))
            )
        if not rows:
            return None
        want = language or self.default_language
        row = next((r for r in rows if r.language == want), rows[0])
        return ResolvedTemplate(
            name=row.name,
            language=row.language,
            category=row.category,
            param_count=row.param_count if (row.body or row.param_count) else None,
            waba_id=row.waba_id,
        )

    def list_templates(self, waba_id: str) -> list[dict[str, Any]]:
        """Every known template on the account, best state per (name, language)."""
        T = self._T
        with self.storage.session() as db:
            rows = list(db.scalars(select(T).where(T.waba_id == waba_id).order_by(T.name, T.language)))
        return [
            {
                "name": r.name,
                "language": r.language,
                "category": r.category,
                "status": r.status,
                "param_count": r.param_count,
                "reason": r.reason,
                "sendable": r.status == "approved",
            }
            for r in rows
        ]

    # -- writes -----------------------------------------------------------------------------

    def record(
        self,
        db: Session,
        *,
        waba_id: str,
        name: str,
        language: str,
        category: str,
        status: str,
        provider_template_id: str = "",
        body: str = "",
        reason: str | None = None,
    ) -> Any:
        """Insert or update one template row inside ``db``'s transaction (race-safe)."""
        T = self._T
        where = (T.waba_id == waba_id, T.name == name, T.language == language)
        row = db.scalar(select(T).where(*where))
        if row is None:
            try:
                with db.begin_nested():
                    row = T(waba_id=waba_id, name=name, language=language)
                    db.add(row)
                    db.flush()
            except IntegrityError:  # pragma: no cover - only under concurrent writers
                row = db.scalar(select(T).where(*where))
        assert row is not None
        row.category = category
        row.status = status
        row.updated_at = utcnow()
        if provider_template_id:
            row.provider_template_id = provider_template_id
        if body:
            row.body = body
            row.param_count = count_variables(body)
        if reason is not None:
            row.reason = reason
        db.flush()
        return row

    def add_approved(
        self, waba_id: str, name: str, *, category: str, param_count: int, language: str = "en"
    ) -> None:
        """Record a template you know is approved (created by hand in WhatsApp Manager, say)."""
        with self.storage.session() as db:
            row = self.record(
                db, waba_id=waba_id, name=name, language=language, category=category, status="approved"
            )
            row.param_count = param_count
            db.commit()

    def sync(self, provider: Provider, waba_id: str, token: str) -> list[dict[str, str]]:
        """Pull every template's review state from the provider and store it.

        Raises :class:`~greenline.errors.ProviderError` when the provider can't be read.
        """
        remote = provider.list_templates(waba_id, token)
        out = []
        with self.storage.session() as db:
            for t in remote:
                status = normalise_status(t.get("status", ""))
                if not status or not t.get("name"):
                    continue
                self.record(
                    db,
                    waba_id=waba_id,
                    name=t["name"],
                    language=t.get("language") or self.default_language,
                    category=(t.get("category") or "utility").lower(),
                    status=status,
                    provider_template_id=str(t.get("id", "")),
                    body=t.get("body", ""),
                    reason=t.get("reason", ""),
                )
                out.append({"name": t["name"], "language": t.get("language", ""), "status": status})
            db.commit()
        return out

    def submit(
        self, provider: Provider, waba_id: str, token: str, definitions: Iterable[TemplateDefinition]
    ) -> list[dict[str, Any]]:
        """Submit definitions for review. Idempotent.

        Syncs first, then skips names the account already holds as approved, pending, paused or
        disabled. Approval is asynchronous with Meta (it arrives by webhook or a later sync), so
        this reports what was submitted, not what can be sent.
        """
        try:
            self.sync(provider, waba_id, token)
        except Exception as exc:  # still submit; Meta refuses true duplicates itself
            log.warning("template sync before submit failed for %s: %s", waba_id, exc)
        T = self._T
        with self.storage.session() as db:
            known: dict[tuple[str, str], str] = {}
            for row in db.scalars(select(T).where(T.waba_id == waba_id)):
                key = (row.name, row.language)
                if _RANK.get(row.status, 0) > _RANK.get(known.get(key, ""), 0):
                    known[key] = row.status
        results = []
        for d in definitions:
            have = known.get((d.name, d.language))
            if have in _ALREADY_SUBMITTED:
                results.append({"name": d.name, "status": have, "submitted": False, "error": ""})
                continue
            try:
                res = provider.create_template(
                    waba_id,
                    token,
                    name=d.name,
                    language=d.language,
                    category=d.category,
                    components=d.components(),
                )
            except Exception as exc:
                log.warning("template %s submit failed on %s: %s", d.name, waba_id, exc)
                results.append(
                    {"name": d.name, "status": have or "not_submitted", "submitted": False, "error": str(exc)}
                )
                continue
            status = normalise_status(res.get("status", "")) or "pending"
            with self.storage.session() as db:
                self.record(
                    db,
                    waba_id=waba_id,
                    name=d.name,
                    language=d.language,
                    category=d.category.lower(),
                    status=status,
                    provider_template_id=res.get("id", ""),
                    body=d.body,
                )
                db.commit()
            results.append({"name": d.name, "status": status, "submitted": True, "error": ""})
        return results

    # -- webhook events ---------------------------------------------------------------------

    def apply_event(self, db: Session, event: WebhookEvent) -> bool:
        """Apply a template status or category webhook to rows on that account only."""
        if not event.waba_id or not (event.template_name or event.template_id):
            return False
        T = self._T
        q = select(T).where(T.waba_id == event.waba_id)
        rows = (
            list(db.scalars(q.where(T.provider_template_id == event.template_id)))
            if event.template_id
            else []
        )
        if not rows and event.template_name:
            cond = [T.name == event.template_name]
            if event.template_language:
                cond.append(T.language == event.template_language)
            rows = list(db.scalars(q.where(*cond)))
        if event.kind == "template_category":
            if event.category not in ("marketing", "utility", "authentication"):
                return False
            for row in rows:
                row.category = event.category
                row.updated_at = utcnow()
            if rows:
                log.warning(
                    "Meta re-filed template %s on %s: %s", event.template_name, event.waba_id, event.reason
                )
            return bool(rows)
        status = normalise_status(event.template_status)
        if not status:
            return False
        if not rows and event.template_name:
            self.record(
                db,
                waba_id=event.waba_id,
                name=event.template_name,
                language=event.template_language or self.default_language,
                category="utility",
                status=status,
                provider_template_id=event.template_id,
                reason=event.reason,
            )
            return True
        for row in rows:
            row.status = status
            row.reason = event.reason
            row.updated_at = utcnow()
            if event.template_id and not row.provider_template_id:
                row.provider_template_id = event.template_id
        return bool(rows)
