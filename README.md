<p align="center"><img src="https://raw.githubusercontent.com/ideatorlabs/.github/main/assets/avatar.png" width="72" alt="Ideator Labs"></p>
<h1 align="center">Greenline</h1>
<p align="center"><b>A self-hostable, multi-tenant engine for the WhatsApp Business Cloud API</b><br><sub>An open-source package by <a href="https://github.com/ideatorlabs">Ideator Labs</a></sub></p>
<p align="center">
<a href="https://github.com/ideatorlabs/greenline/actions/workflows/ci.yml"><img src="https://github.com/ideatorlabs/greenline/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
<img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
</p>

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

## Concepts

```
            your app                                   Meta
   ┌────────────────────────┐                ┌──────────────────────┐
   │ engine.send(...)       │                │  Cloud API (Graph)   │
   │ engine.hooks.on(...)   │                └─────────▲──────┬─────┘
   └──────────┬─────────────┘                          │      │ webhooks
              ▼                                        │      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ Engine                                                            │
   │  gate (kill switch, test mode) → idempotency → consent & window   │
   │  → number protection → reserve row → provider.send (retries)      │
   │                                                                   │
   │  WebhookHandler: verify signature → parse → dedup → apply → hooks │
   ├───────────────┬──────────────────────┬────────────────────────────┤
   │ Provider port │  TemplateRegistry    │  Storage (SQLAlchemy)      │
   │ meta_cloud    │  submit/sync/resolve │  numbers, contacts,        │
   │ mock / yours  │                      │  messages, templates, ...  │
   └───────────────┴──────────────────────┴────────────────────────────┘
```

- **Tenant**: one business you send for, identified by your own string id. Greenline never needs
  your tenant table.
- **Number**: a sending phone number (`phone_number_id` + WABA + token). With a `tenant_id` it
  belongs to that tenant; without one it is shared by every tenant that has no number of its own.
- **Contact**: one person on one number, with consent state and the service-window expiry.
- **Window**: an inbound message opens a 24-hour customer-service window. Inside it, free-form text
  is allowed; outside it, only approved templates.
- **Skip reasons**: `send()` never raises for policy. A refused send returns `ok=False` and
  `skipped` set to one of `no_phone`, `empty_message`, `sending_paused`, `test_mode`, `opted_out`,
  `not_opted_in`, `outside_window`, `no_approved_template:<name>`, `duplicate`, `daily_cap`,
  `quality_paused`, `budget_exhausted`. It raises `NumberNotReady` when the tenant has no number.

### Hook payloads

| Event | Keyword arguments |
|---|---|
| `inbound` | `tenant_id, phone, phone_number_id, external_id, text, message_type, button_payload, provider_message_id, message_id` |
| `status` | `tenant_id, phone, status, error_code, provider_message_id, message_id` |
| `opt_out` / `opt_in` | `tenant_id, phone, phone_number_id, external_id` |
| `template_status` | `waba_id, name, language, status, reason` |
| `template_category` | `waba_id, name, language, category` |
| `quality` | `waba_id, display_phone, quality, tier` |

`phone` is digits only (E.164 without `+`), as Meta sends it. `tenant_id` is `""` when an inbound
message cannot be attributed, or for a receipt about a message Greenline did not send. Accept
`**kwargs` in subscribers so new keys do not break them.

## Configuration

Pass a `Settings(...)` or call `Settings.from_env()`; each field maps to `GREENLINE_<FIELD>`.

| Variable | Default | Meaning |
|---|---|---|
| `GREENLINE_PROVIDER` | `mock` | `mock`, `meta_cloud`, or a name you registered |
| `GREENLINE_GRAPH_BASE_URL` | `https://graph.facebook.com` | Graph API base URL |
| `GREENLINE_GRAPH_VERSION` | `v24.0` | Graph API version; Meta retires versions, keep it current |
| `GREENLINE_HTTP_TIMEOUT` | `15` | seconds per provider call |
| `GREENLINE_APP_SECRET` | | Meta app secret, verifies webhook signatures (none = every POST is rejected) |
| `GREENLINE_VERIFY_TOKEN` | | webhook GET handshake token |
| `GREENLINE_FALLBACK_TOKEN` | | system-user token for numbers without one, and when a number's token expires |
| `GREENLINE_TOKEN_KEY` | | Fernet key for stored tokens (`greenline.generate_key()`) |
| `GREENLINE_REQUIRE_TOKEN_KEY` | `false` | refuse to store tokens without encryption |
| `GREENLINE_WEBHOOK_PATH` | `/webhooks/whatsapp` | route used by `mount()` |
| `GREENLINE_DEFAULT_LANGUAGE` | `en` | preferred template language |
| `GREENLINE_SENDING_ENABLED` | unset | master switch; unset = on for `mock`, off for real providers |
| `GREENLINE_TEST_MODE` | `false` | deliver only to the allow list |
| `GREENLINE_TEST_RECIPIENTS` | | comma-separated allow list (matched on the last 10 digits) |
| `GREENLINE_MAX_RETRIES` | `3` | retries for retryable provider errors |
| `GREENLINE_RETRY_BUDGET_SECONDS` | `5` | cap on total time slept between retries |
| `GREENLINE_ALLOW_MARKETING_WITHOUT_OPT_IN` | `false` | skip the opt-in check for marketing |
| `GREENLINE_INBOUND_COUNTS_AS_OPT_IN` | `true` | a first message from someone records an opt-in |
| `GREENLINE_DAILY_CAP_PER_NUMBER` | `0` | business-initiated sends per number per 24 h (0 = off) |
| `GREENLINE_MONTHLY_BUDGET_MINOR` | `0` | pause marketing at this month's spend, minor units (0 = off) |
| `GREENLINE_WINDOW_HOURS` | `24` | customer-service window length |
| `GREENLINE_ATTRIBUTION_DAYS` | `30` | shared-number reply attribution window |
| `GREENLINE_STOP_KEYWORDS` / `GREENLINE_START_KEYWORDS` | see `config.py` | whole-message keywords |

Engine-only options (constructor arguments): `rate_card`, `test_recipients_for(tenant_id)`,
`tenant_resolver(event)`, `clock`, `sleep`. `Storage(url, table_prefix="greenline_")` sets the table
prefix.

### Pricing

`RateCard.example_inr()` loads `data/rate_card.example-inr.json`, an **example** card in paise.
Meta changes prices and they vary by market: verify current Meta pricing and pass your own
`RateCard(currency=..., rates={...})` before using the numbers for billing. Costs are estimates
stored per message; `engine.usage()` sums them per tenant.

## Extending

Add a provider (for example a BSP) by implementing four methods:

```python
from greenline import Outbound, ProviderResult, Sender, register_provider
from greenline.providers.meta_cloud import parse_webhook_payload


class MyBspProvider:
    name = "my_bsp"

    def send(self, sender: Sender, message: Outbound) -> ProviderResult:
        ...  # call the BSP; raise ProviderError(retryable=...) or RateLimited on failure
        return ProviderResult(ok=True, provider_message_id="...", status="sent")

    def parse_webhook(self, payload: dict) -> list:
        return parse_webhook_payload(payload)  # if the BSP forwards Meta's format

    def create_template(self, waba_id, token, *, name, language, category, components) -> dict: ...

    def list_templates(self, waba_id, token) -> list[dict]: ...


register_provider("my_bsp", lambda settings, **_: MyBspProvider())
```

Other frameworks: `engine.webhook.verify(query_params)` and
`engine.webhook.receive(raw_body, signature_header)` return `(status, body)`; wrap them in a
Flask, Django or Starlette view in a few lines. Use `engine.storage.metadata` as Alembic's
`target_metadata` if you manage migrations.

## Running the example

`examples/fastapi_app.py` is a complete app (FastAPI, SQLite, mock provider, a template, hooks):

```bash
uv venv && uv pip install -e ".[fastapi]"
python examples/fastapi_app.py            # scripted walkthrough over HTTP, no server needed
uv pip install uvicorn && uvicorn examples.fastapi_app:app --reload   # or serve it
```

The walkthrough prints the handshake, a signed inbound message, STOP refusing a send, START, an
in-window text, the template fallback 25 hours later, receipts that never move backwards, a
rejected bad signature, and the month's usage. `greenline.testing` has the helpers it uses to
build and sign Meta-shaped webhook bodies.

## Testing

```bash
uv venv --python 3.11 .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/pytest --cov=greenline
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

Tests never touch the network: the Meta adapter runs against `httpx.MockTransport` with exact
request bodies asserted, and any real HTTP call fails the suite.

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

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately as described in
[SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
