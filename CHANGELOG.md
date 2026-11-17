# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-17

### Added

- `Engine` with `send()` (free-form text inside the customer-service window, approved template
  outside it), `send_text()` and `send_template()`.
- Template parameter sanitising and fitting (`clean_param`, `fit_params`).
- Idempotency keys reserved before the provider call.
- Master kill switch and test mode with recipient allow lists.
- Consent handling: whole-message STOP/START keywords, stop quick-reply payloads, marketing opt-in.
- Monotonic delivery status and shared-number reply attribution.
- Number protection: daily cap, red-quality marketing hold, monthly marketing budget.
- Signed webhook handling (GET handshake, `X-Hub-Signature-256`), event de-duplication.
- Hooks: `inbound`, `status`, `opt_out`, `opt_in`, `template_status`, `template_category`, `quality`.
- Providers: `meta_cloud` (Graph API over httpx) and `mock`; provider registry.
- Template registry: define, submit, sync, resolve, webhook updates.
- SQLAlchemy 2.0 storage with configurable table prefix; Fernet token encryption.
- Rate card cost estimates and per-tenant usage; example INR rate card (example only).
- Optional FastAPI integration (`greenline.ext.fastapi.mount`).
- `greenline.testing` helpers for Meta-shaped, signed webhook bodies.

[Unreleased]: https://github.com/ideatorlabs/greenline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ideatorlabs/greenline/releases/tag/v0.1.0
