"""FastAPI integration (``pip install "greenline[fastapi]"``).

from fastapi import FastAPI
from greenline.ext.fastapi import mount

app = FastAPI()
mount(app, engine)          # GET + POST at engine.settings.webhook_path
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from greenline.webhook import SIGNATURE_HEADER

if TYPE_CHECKING:
    from greenline.engine import Engine


def build_router(engine: Engine, path: str | None = None) -> APIRouter:
    """A router with the webhook's GET (handshake) and POST (events) routes."""
    route = path or engine.settings.webhook_path
    router = APIRouter()

    @router.get(route, include_in_schema=False)
    async def verify(request: Request) -> Response:
        status, body = engine.webhook.verify(request.query_params)
        return PlainTextResponse(body, status_code=status)

    @router.post(route, include_in_schema=False)
    async def receive(request: Request) -> Response:
        raw = await request.body()
        # the engine does blocking database work: keep it off the event loop
        status, body = await run_in_threadpool(
            engine.webhook.receive, raw, request.headers.get(SIGNATURE_HEADER)
        )
        return JSONResponse(body, status_code=status)

    return router


def mount(app: FastAPI, engine: Engine, path: str | None = None) -> APIRouter:
    """Add the webhook routes to ``app`` and return the router."""
    router = build_router(engine, path)
    app.include_router(router)
    return router
