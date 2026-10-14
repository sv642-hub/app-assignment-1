"""FastAPI service skeleton for DSAN 6700 Assignment 1.

Exposes a typed /health endpoint and a /predict endpoint backed by a local
language model, with input and output validated/serialized by Pydantic models.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app_assignment_1 import __version__
from app_assignment_1.config import get_settings
from app_assignment_1.model import TextGenerator

# Validate configuration at startup: if the environment is misconfigured this
# raises immediately and the process refuses to start.

settings = get_settings()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Reuse the cached settings. Tests can clear the cache before startup.
    model_settings = get_settings()
    try:
        app.state.generator = TextGenerator(
            model_name=model_settings.llm_model_name,
            max_new_tokens=model_settings.llm_max_new_tokens,
        )
    except Exception:
        # Fail fast: a service without its model should not report healthy.
        logger.exception(
            "Failed to load model %r; refusing to start.",
            model_settings.llm_model_name,
        )
        raise
    yield
    del app.state.generator


app = FastAPI(
    title="app-assignment-1",
    version=__version__,
    lifespan=lifespan,
    description="DSAN 6700 App Deployment — Assignment 1 service skeleton.",
    debug=settings.debug,
)


class HealthResponse(BaseModel):
    """Small typed payload confirming the service is up."""

    status: str = "ok"
    version: str = __version__
    environment: str = settings.environment


class PredictRequest(BaseModel):
    """Validated input for the predict endpoint."""

    text: str = Field(
        ..., min_length=1, description="Input text for the language model."
    )


class PredictResponse(BaseModel):
    """Predictable, serialized output for the predict endpoint."""

    prediction: str
    input_length: int


class ErrorResponse(BaseModel):
    """Error payload returned when the model cannot answer."""

    detail: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check for callers, containers, and graders."""
    return HealthResponse()


@app.post(
    "/predict",
    response_model=PredictResponse,
    responses={
        500: {"model": ErrorResponse, "description": "Model generation failed."},
        503: {"model": ErrorResponse, "description": "Model not loaded."},
    },
)
def predict(
    request: PredictRequest,
    http_request: Request,
) -> PredictResponse:
    """Generate a reply with the language model loaded at startup.

    Returns 503 if the model is not loaded and 500 if generation fails. The
    underlying error is logged on the server and never returned to the caller.
    """
    generator: TextGenerator | None = getattr(http_request.app.state, "generator", None)
    if generator is None:
        raise HTTPException(status_code=503, detail="Model not loaded.")

    try:
        answer = generator.generate(request.text)
    except Exception as exc:
        logger.exception("Model generation failed.")
        raise HTTPException(status_code=500, detail="Model generation failed.") from exc

    return PredictResponse(
        prediction=answer,
        input_length=len(request.text),
    )
