# Development

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
