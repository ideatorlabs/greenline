# Configuration

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

## Pricing

`RateCard.example_inr()` loads `data/rate_card.example-inr.json`, an **example** card in paise.
Meta changes prices and they vary by market: verify current Meta pricing and pass your own
`RateCard(currency=..., rates={...})` before using the numbers for billing. Costs are estimates
stored per message; `engine.usage()` sums them per tenant.
