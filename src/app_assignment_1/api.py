"""Smart AI Gateway mocked midpoint API."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from app_assignment_1.config import Settings, get_settings
from app_assignment_1.release import Release, ReleaseChange, ReleaseStatus, VersionInfo
from app_assignment_1.router import MockJevRouter, Router
from app_assignment_1.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    JevBreakerStatus,
    RecentRequest,
)
from app_assignment_1.service import BackendUnavailable, Gateway, ReleaseNotSaved
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
        gateway = Gateway(
            configured,
            router or MockJevRouter(configured.intents_path),
            store=opened,
        )
        await opened.open()
        await gateway.load_release()
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
                    version_id=r.trace.version_id,
                    canary=r.trace.canary,
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
        )

    @app.get("/v1/release", response_model=ReleaseStatus)
    async def get_release(request: Request) -> ReleaseStatus:
        gateway: Gateway = request.app.state.gateway
        return ReleaseStatus(
            current=gateway.release,
            versions=[
                VersionInfo(id=v.id, description=v.description)
                for v in gateway.versions
            ],
            history=await gateway.store.recent_releases(20),
        )

    @app.put(
        "/v1/release",
        response_model=Release,
        responses={
            401: {"description": "Missing or wrong operator token"},
            403: {"description": "Release changes are turned off"},
            422: {"description": "Invalid release or unknown version"},
            503: {"description": "The release could not be saved"},
        },
    )
    async def put_release(
        change: ReleaseChange,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Release:
        # /v1 is public through Caddy, so a change needs the operator token.
        token = request.app.state.operator_token
        if token is None:
            raise HTTPException(
                403, "Release changes are turned off; set APP_OPERATOR_TOKEN"
            )
        expected = f"Bearer {token.get_secret_value()}"
        if not secrets.compare_digest(
            (authorization or "").encode(), expected.encode()
        ):
            raise HTTPException(
                401, "Missing or wrong operator token", {"WWW-Authenticate": "Bearer"}
            )
        gateway: Gateway = request.app.state.gateway
        try:
            return await gateway.set_release(change)
        except ValueError as exc:
            # Only "Unknown version: <id>", which names a version, not a secret.
            raise HTTPException(422, str(exc)) from None
        except ReleaseNotSaved:
            raise HTTPException(503, "The release could not be saved") from None

    # Internal only: Caddy does not proxy /metrics, and it stays out of /v1 and
    # the OpenAPI docs. Prometheus scrapes it on the Compose network.
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        gateway: Gateway = request.app.state.gateway
        return Response(gateway.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
