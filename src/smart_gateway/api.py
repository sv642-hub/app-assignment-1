"""FastAPI gateway for the Smart AI Gateway prototype.

Exposes POST /v1/chat, GET /v1/requests/{id}, and GET /v1/health, with input
and output validated/serialized by Pydantic models. Jev and the Qwen models are
mocked from the JSON files in config/.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from smart_gateway import __version__
from smart_gateway.catalog import load_catalog
from smart_gateway.config import get_settings
from smart_gateway.gateway import Gateway, RequestStore
from smart_gateway.router import MockJevRouter
from smart_gateway.schemas import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HealthResponse,
)

# Validate configuration at startup: if the environment is misconfigured this
# raises immediately and the process refuses to start.
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Reuse the cached settings. Tests can clear the cache before startup.
    gateway_settings = get_settings()
    catalog = load_catalog(
        gateway_settings.intents_path,
        gateway_settings.policy_cards_path,
        gateway_settings.backends_path,
    )
    app.state.gateway = Gateway(
        router=MockJevRouter(catalog.intents),
        catalog=catalog,
        store=RequestStore(gateway_settings.request_store_size),
    )
    yield
    del app.state.gateway


app = FastAPI(
    title="Smart AI Gateway",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Mocked prototype of the Smart AI Gateway (DSAN 6700). Routes a banking "
        "customer message with a mock of Jev, answers from a policy card with a "
        "mock Qwen backend, and returns a trace."
    ),
    debug=settings.debug,
)

UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    503: {"model": ErrorResponse, "description": "Gateway not started."}
}


def _gateway(request: Request) -> Gateway:
    gateway: Gateway | None = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="Gateway not started.")
    return gateway


@app.get("/v1/health", response_model=HealthResponse, responses=UNAVAILABLE)
def health(request: Request) -> HealthResponse:
    """Liveness check for callers, containers, and the proxy."""
    gateway = _gateway(request)
    return HealthResponse(
        environment=get_settings().environment,
        router=gateway.router.name,
        intents=len(gateway.catalog.intents),
        backends=[backend.name for backend in gateway.backends.values()],
    )


@app.post("/v1/chat", response_model=ChatResponse, responses=UNAVAILABLE)
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Detect the intent, answer from its policy card, and return the trace.

    Messages that match no intent get intent "unknown" and a polite reply from
    the rule router.
    """
    return await _gateway(request).chat(body.message)


@app.get(
    "/v1/requests/{request_id}",
    response_model=ChatResponse,
    responses={
        404: {"model": ErrorResponse, "description": "No such request."},
        **UNAVAILABLE,
    },
)
def get_request(request_id: str, request: Request) -> ChatResponse:
    """Look up a recent /v1/chat response by its request_id."""
    response = _gateway(request).store.get(request_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    return response
