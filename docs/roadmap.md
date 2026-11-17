# Limits & roadmap

## Limitations and non-goals

- **Number onboarding is not included.** Register numbers you already set up in Meta's tools (or
  through Embedded Signup in your own UI) with `engine.add_number(...)`.
- **One opt-out per number.** On a shared number, a STOP opts the person out of every tenant using
  that number.
- **Text and templates only.** Free-form media, interactive messages and carousels are not sent by
  the engine (templates can carry a header image or document and button parameters).
- **Synchronous.** Sends and webhook processing run in the caller's thread (the FastAPI router
  moves them to a thread pool). At high volume, put a queue in front of `engine.send` and
  `engine.process_webhook`.
- **Retries sleep in the caller's thread**, capped by `retry_budget_seconds`.
- **Cost figures are estimates**, not Meta's invoice.

## Roadmap

- Async engine and an asyncio provider.
- Business profile updates and media upload through the provider port.
- Optional per-tenant opt-out scope on shared numbers.
- Alembic migration templates.
