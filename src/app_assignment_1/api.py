"""Smart AI Gateway mocked midpoint API."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import SecretStr

from app_assignment_1.config import Settings, get_settings
from app_assignment_1.router import MockJevRouter, Router
from app_assignment_1.schemas import (
    BackendConfig,
    BackendMonitor,
    BackendName,
    ChatRequest,
    ChatResponse,
    FaultRequest,
    HealthResponse,
    JevBreakerStatus,
    OperatorRequest,
    RecentRequest,
)
from app_assignment_1.service import (
    BackendUnavailable,
    Gateway,
    LastUsableBackend,
    NotAMock,
)
from app_assignment_1.store import Store, create_store

bearer = HTTPBearer(auto_error=False)


def require_operator(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    """Allow a request only with `Authorization: Bearer <APP_OPERATOR_TOKEN>`."""
    token: SecretStr | None = request.app.state.operator_token
    if token is None:
        raise HTTPException(
            503, "Operator routes are off: APP_OPERATOR_TOKEN is not set"
        )
    given = credentials.credentials if credentials else ""
    if not secrets.compare_digest(given.encode(), token.get_secret_value().encode()):
        raise HTTPException(
            401, "Missing or wrong operator token", {"WWW-Authenticate": "Bearer"}
        )


def create_app(
    settings: Settings | None = None,
    router: Router | None = None,
    store: Store | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured = settings or get_settings()
        opened = store or create_store(configured)
        gateway = Gateway(
            configured,
            router or MockJevRouter(configured.intents_path),
            store=opened,
        )
        await opened.open()
        app.state.gateway = gateway
        app.state.operator_token = configured.operator_token
        try:
            yield
        finally:
            # Save anything noticed since the last request, such as a re-enable
            # seen by a /metrics scrape.
            await gateway.save_interventions()
            await opened.close()

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
        result = await gateway.store.get_request(request_id)
        if result is None:
            raise HTTPException(404, "Request not found or no longer retained")
        return result

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        gateway: Gateway = request.app.state.gateway
        breaker = gateway.jev_breaker
        # Only the backends decide the status. While the Jev breaker is open the
        # rule router routes, and customers still get answers from the right
        # models; `jev_breaker` shows it.
        healthy = all(gateway.usable(b) for b in gateway.backends)
        monitor = gateway.controller.stats()
        # stats() may log a re-enable; save it before reading the log back.
        await gateway.save_interventions()
        return HealthResponse(
            status="ok" if healthy else "degraded",
            recent_requests=[
                RecentRequest(
                    completed_at=r.completed_at,
                    route=r.trace.route,
                    model=r.trace.model,
                    fallback_reason=r.trace.fallback_reason,
                    format_check=r.trace.format_check,
                )
                for r in await gateway.store.recent_requests(20)
            ],
            usage=list(gateway.usage.values()),
            backends=[b.config for b in gateway.backends],
            monitor=monitor,
            interventions=await gateway.store.recent_interventions(50),
            controller=gateway.controller.rules(),
            jev_breaker=JevBreakerStatus(
                state=breaker.state(),
                consecutive_failures=breaker.consecutive_failures,
                failure_limit=breaker.failure_limit,
                cooldown_seconds=breaker.cooldown,
            ),
            semantic=gateway.semantic.stats(),
            semantic_rules=gateway.semantic.rules,
        )

    # Operator routes. Caddy proxies /v1, so the token is what guards them.
    @app.post(
        "/v1/operator/models/{name}/disable",
        response_model=list[BackendMonitor],
        dependencies=[Depends(require_operator)],
        responses={409: {"description": "It is the last usable model"}},
    )
    async def operator_disable(
        name: BackendName, request: Request, payload: OperatorRequest | None = None
    ) -> list[BackendMonitor]:
        """Hold a model disabled until an operator enables it. No cooldown."""
        gateway: Gateway = request.app.state.gateway
        try:
            gateway.operator_disable(name, (payload or OperatorRequest()).reason)
        except LastUsableBackend:
            raise HTTPException(409, f"{name} is the last usable model") from None
        await gateway.save_interventions()
        return gateway.controller.stats()

    @app.post(
        "/v1/operator/models/{name}/enable",
        response_model=list[BackendMonitor],
        dependencies=[Depends(require_operator)],
    )
    async def operator_enable(
        name: BackendName, request: Request, payload: OperatorRequest | None = None
    ) -> list[BackendMonitor]:
        """Lift an operator hold or a controller cooldown, with a fresh window."""
        gateway: Gateway = request.app.state.gateway
        gateway.operator_enable(name, (payload or OperatorRequest()).reason)
        await gateway.save_interventions()
        return gateway.controller.stats()

    @app.post(
        "/v1/faults",
        response_model=BackendConfig,
        dependencies=[Depends(require_operator)],
        responses={409: {"description": "The model is not a mock"}},
    )
    async def set_fault(payload: FaultRequest, request: Request) -> BackendConfig:
        """Set a simulated fault on a mock model while it runs. Lost on restart;
        the Compose overrides set faults at startup."""
        gateway: Gateway = request.app.state.gateway
        try:
            return gateway.set_fault(payload)
        except NotAMock:
            raise HTTPException(409, f"{payload.model} is not a mock model") from None

    # Internal only: Caddy does not proxy /metrics, and it stays out of /v1 and
    # the OpenAPI docs. Prometheus scrapes it on the Compose network.
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        gateway: Gateway = request.app.state.gateway
        return Response(gateway.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
