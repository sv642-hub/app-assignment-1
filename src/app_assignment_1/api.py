"""Smart AI Gateway mocked midpoint API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

from app_assignment_1.config import Settings, get_settings
from app_assignment_1.router import MockJevRouter, Router
from app_assignment_1.schemas import ChatRequest, ChatResponse
from app_assignment_1.service import BackendUnavailable, Gateway


def create_app(
    settings: Settings | None = None, router: Router | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured = settings or get_settings()
        mock = MockJevRouter(configured.intents_path)
        gateway = Gateway(configured, router or mock)
        if set(gateway.cards) != {item.intent for item in mock.intents}:
            raise ValueError("Policy cards must match the configured intents exactly")
        app.state.gateway = gateway
        yield

    app = FastAPI(
        title="Smart AI Gateway (mock prototype)",
        version="0.2.0",
        description="Fictional policy replies. No Jev, Qwen or GPU calls.",
        lifespan=lifespan,
    )

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        responses={503: {"description": "No backend available"}},
    )
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        gateway: Gateway = request.app.state.gateway
        try:
            return await gateway.chat(payload.message)
        except BackendUnavailable as exc:
            raise HTTPException(503, "No mock backend is available") from exc

    @app.get("/v1/requests/{request_id}", response_model=ChatResponse)
    async def get_request(request_id: UUID, request: Request) -> ChatResponse:
        gateway: Gateway = request.app.state.gateway
        result = gateway.requests.get(request_id)
        if result is None:
            raise HTTPException(404, "Request not found or no longer retained")
        return result

    @app.get("/v1/health")
    async def health(request: Request) -> dict:
        gateway: Gateway = request.app.state.gateway
        return {
            "status": "ok"
            if any(b.available for b in gateway.backends)
            else "degraded",
            "mode": "mock",
            "router": "mock_jev",
            "backends": [b.model_dump() for b in gateway.backends],
        }

    return app


app = create_app()
