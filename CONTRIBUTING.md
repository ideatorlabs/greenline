# Contributing to Greenline

Thanks for helping. Bug reports, fixes, docs and new provider adapters are all welcome.

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install -p .venv -e ".[dev]"
```

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest --cov=greenline
```

Tests must not use the network or real credentials. Fake HTTP with `httpx.MockTransport`; build
webhook bodies with `greenline.testing`.

## Pull requests

- One topic per pull request, with a test that fails without the change.
- Keep the public API typed and documented; update `README.md` and `CHANGELOG.md` when behaviour
  changes.
- CI must pass on Python 3.10 to 3.13.
- No Contributor License Agreement or DCO sign-off is required. By submitting a pull request you
  agree your contribution is licensed under Apache-2.0.
