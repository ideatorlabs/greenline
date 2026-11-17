---
hide:
  - navigation
---

<div class="il-hero" markdown>
<img src="assets/brand/icon-512.png" alt="" width="112" height="112">
<div markdown>
# Greenline
<p class="il-tagline">Self-hosted WhatsApp Cloud API engine</p>
<p class="il-by">An open-source package by <a href="https://ideatorlabs.github.io/">Ideator Labs</a></p>
[Get started](#install){ .md-button .md-button--primary }
[GitHub](https://github.com/ideatorlabs/greenline){ .md-button }
</div>
</div>

## Why it exists

Calling the WhatsApp Cloud API is easy; running it for many businesses without getting a number
banned or a customer double-messaged is not. Every product that sends on behalf of others ends up
rebuilding the same rules: the 24-hour customer-service window, template fallbacks, STOP/START,
idempotency, receipts that arrive out of order, signed webhooks, per-tenant numbers and tokens, and
a cost meter. Greenline packages those rules as a small Python library you run inside your own app.

Greenline is not affiliated with, endorsed by, or sponsored by Meta. WhatsApp is a trademark of Meta
Platforms, Inc.

## Features

- **One `send()` call** that picks free-form text inside the customer-service window and falls back
  to an approved template outside it.
- **Template parameter hygiene** for Meta's rules: newlines and tabs become spaces, runs of 4+
  spaces collapse, values are trimmed and capped, empty values become `-`, and the list is padded
  or truncated to the template's variable count.
- **Consent**: whole-message STOP/START keywords (and a "stop" quick-reply payload), marketing only
  with opt-in, and a STOP can only be undone by the person's own START.
- **Idempotency keys**, reserved before the provider is called, so concurrent duplicates cannot
  both send; a failed attempt frees its key, an "unknown delivery" keeps it.
- **Monotonic delivery status**: `queued < sent < delivered < read`; `failed` only replaces
  `queued` or `sent`.
- **Multi-tenant**: each tenant can have its own number and access token (encrypted at rest with
  Fernet), or share a number. Inbound messages on a shared number are attributed to the tenant that
  last messaged that person within a window (or to whoever your resolver says).
- **Safety switches**: a master kill switch that defaults to *off* for real providers, and a test
  mode that only delivers to allow-listed numbers.
- **Number protection**: optional per-number daily cap, marketing held while Meta rates the number
  red, optional monthly marketing budget.
- **Signed webhooks**: GET handshake, `X-Hub-Signature-256` HMAC verification (fail closed),
  de-duplication of Meta retries, always-200 for anything that is not a signature failure.
- **Hooks** (`inbound`, `status`, `opt_out`, `opt_in`, `template_status`, `template_category`,
  `quality`) fired after the database commit.
- **Templates**: define, submit (idempotent), sync review states, resolve the approved one at send
  time; webhook updates for approvals and category re-filings.
- **Cost meter**: per-message estimates from a configurable rate card, usage per tenant.
- **Providers as a port**: `meta_cloud` (Graph API over httpx, configurable base URL and version)
  and `mock`; add your own BSP adapter in one class.
- **Storage**: SQLAlchemy 2.0 models with a configurable table prefix, SQLite or PostgreSQL, no
  foreign keys into your tables.
- **FastAPI** integration as an optional extra: `mount(app, engine)`.

## Install

```bash
pip install greenline             # core: SQLAlchemy, httpx, cryptography
pip install "greenline[fastapi]"  # plus the FastAPI webhook router
```

> Until the first PyPI release, install straight from GitHub:
> `pip install "greenline @ git+https://github.com/ideatorlabs/greenline"`


Or with uv: `uv pip install "greenline[fastapi]"`. Bring your own database driver for
PostgreSQL (for example `psycopg`).

## 60-second quickstart

This runs as is (it is `examples/quickstart.py`; the test suite executes it):

```python
from greenline import Engine, MockProvider, Settings, Storage, TemplateDefinition, testing

storage = Storage("sqlite://")  # or "postgresql+psycopg://user:pass@host/db"
storage.create_all()
engine = Engine(storage, MockProvider(), Settings(provider="mock"))

# One tenant with its own sending number, and a template approved on its account.
engine.add_number(tenant_id="store-1", phone_number_id="PN-1", waba_id="WABA-1", token="access-token")
engine.submit_templates(
    "WABA-1",
    [
        TemplateDefinition(
            name="order_ready",
            category="utility",
            body="Hi {{1}}, your order {{2}} is ready. Thank you for shopping with us.",
        )
    ],
)


@engine.hooks.on("inbound")
def on_inbound(tenant_id: str, phone: str, text: str, **_: object) -> None:
    print(f"[{tenant_id}] {phone}: {text}")


# The customer has not written to us, so the approved template goes out.
r = engine.send(
    tenant_id="store-1",
    to="+1 555 0101",
    text="Your order is ready",
    template="order_ready",
    params=["Sam", "#1042"],
)
print(r.kind, r.status)  # template sent

# They reply (normally through the webhook), which opens the 24-hour window: free-form text now.
engine.process_webhook(testing.inbound_payload(phone_number_id="PN-1", phone="15550101", text="Thanks!"))
r = engine.send(tenant_id="store-1", to="+1 555 0101", text="You're welcome!", template="order_ready")
print(r.kind, r.status)  # text sent
```

For production, switch the provider and set the secrets:

```python
import os

from fastapi import FastAPI

from greenline import Engine, Settings, Storage
from greenline.ext.fastapi import mount

settings = Settings.from_env()  # GREENLINE_PROVIDER=meta_cloud, GREENLINE_APP_SECRET=..., see Configuration
storage = Storage(os.environ.get("DATABASE_URL", "sqlite:///greenline.db"))
storage.create_all()
engine = Engine(storage, settings=settings)

app = FastAPI()
mount(app, engine)  # GET + POST /webhooks/whatsapp
```

With `GREENLINE_PROVIDER=meta_cloud`, nothing is sent until you also set `GREENLINE_SENDING_ENABLED=true`.
