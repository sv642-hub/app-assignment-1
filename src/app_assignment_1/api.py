"""FastAPI service skeleton for DSAN 6700 Assignment 1.

Exposes a typed /health endpoint and a placeholder /predict endpoint whose
input and output are validated/serialized by Pydantic models.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from app_assignment_1 import __version__
from app_assignment_1.config import get_settings
from app_assignment_1.model import TextGenerator

# Validate configuration at startup: if the environment is misconfigured this
# raises immediately and the process refuses to start.

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.generator = TextGenerator()
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
    """Validated input for the placeholder predict endpoint."""

    text: str = Field(
        ..., 
        min_length=1, 
        description="Input text for the language model."
    )


class PredictResponse(BaseModel):
    """Predictable, serialized output for the placeholder predict endpoint."""

    prediction: str
    input_length: int


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check for callers, containers, and graders."""
    return HealthResponse()


@app.post("/predict", response_model=PredictResponse)
def predict(
    request: PredictRequest,
    http_request: Request,
) -> PredictResponse:
    generator: TextGenerator = http_request.app.state.generator
    answer = generator.generate(request.text)

    return PredictResponse(
        prediction=answer,
        input_length=len(request.text),
    )