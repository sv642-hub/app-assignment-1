"""Smart AI Gateway mocked midpoint API."""

import math
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST

from app_assignment_1.auth import require_operator
from app_assignment_1.config import Settings, get_settings
from app_assignment_1.jev import TypeSafeJevRouter, create_jev_http_client
from app_assignment_1.labels import load_labels
from app_assignment_1.real_backends import open_vllm_backends
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
    JevFaultConfig,
    JevFaultRequest,
    OperatorRequest,
    RecentRequest,
)
from app_assignment_1.service import (
    BackendUnavailable,
    DeadlineExceeded,
    Gateway,
    LastUsableBackend,
    NotAMock,
)
from app_assignment_1.store import Store, create_store


def create_app(
    settings: Settings | None = None,
    router: Router | None = None,
    store: Store | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured = settings or get_settings()
        opened = store or create_store(configured)
        async with AsyncExitStack() as stack:
            if router is not None:
                chosen: Router = router
            elif configured.jev_mode == "real":
                # One shared client for the app's lifetime, closed on exit.
                client = await stack.enter_async_context(
                    create_jev_http_client(configured)
                )
                chosen = TypeSafeJevRouter(
                    client, configured.jev_model, load_labels(configured.labels_path)
                )
            else:
                chosen = MockJevRouter(configured.intents_path)
            backends = (
                await open_vllm_backends(configured, stack)
                if configured.provider_mode == "real"
                else None
            )
            gateway = Gateway(configured, chosen, backends, store=opened)
            await opened.open()
            app.state.gateway = gateway
            app.state.operator_token = configured.operator_token
            try:
                yield
            finally:
                await gateway.finish_saves()
                await opened.close()

    app = FastAPI(
        title="Smart AI Gateway (mock prototype)",
        version="0.2.0",
        description=(
            "Fictional policy replies from mock Qwen backends. Jev is a local "
            "mock unless APP_JEV_MODE=real, which sends each message to TypeSafe."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """The default 422, except that a NaN or Infinity the client sent is
        echoed as text: JSON can't hold it, and writing it gave a 500."""
        return JSONResponse(
            status_code=422,
            content={"detail": _finite(jsonable_encoder(exc.errors()))},
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
            detail = (
                "deadline_exceeded"
                if isinstance(exc, DeadlineExceeded)
                else "No mock backend is available"
            )
            raise HTTPException(503, detail) from exc

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
            mode="mock"
            if all(getattr(b, "mocked", True) for b in gateway.backends)
            else "real",
            jev_mode="mock" if gateway.jev_mocked else "real",
            router="mock_jev" if gateway.jev_mocked else "jev",
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
            settings_version=gateway.settings_version,
            semantic=gateway.semantic.stats(),
            semantic_rules=gateway.semantic.rules,
            jev_usage=gateway.jev_usage(),
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

    @app.post(
        "/v1/faults/jev",
        response_model=JevFaultConfig,
        dependencies=[Depends(require_operator)],
        responses={409: {"description": "Jev is real, not the mock"}},
    )
    async def set_jev_fault(
        payload: JevFaultRequest, request: Request
    ) -> JevFaultConfig:
        """Set a simulated fault on the mock Jev router while it runs: `outage`
        makes calls fail, `slow` holds them for `seconds` more. Lost on restart."""
        gateway: Gateway = request.app.state.gateway
        try:
            return gateway.set_jev_fault(payload)
        except NotAMock:
            raise HTTPException(409, "Jev is real, not the mock") from None

    # Internal only: Caddy does not proxy /metrics, and it stays out of /v1 and
    # the OpenAPI docs. Prometheus scrapes it on the Compose network.
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        gateway: Gateway = request.app.state.gateway
        return Response(gateway.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


def _finite(value: Any) -> Any:
    """Copy `value` with each non-finite float replaced by its text."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


app = create_app()
