# Concepts

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

## Hook payloads

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
