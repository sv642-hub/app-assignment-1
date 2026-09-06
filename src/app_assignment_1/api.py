"""FastAPI service skeleton for DSAN 6700 Assignment 1.

Exposes a typed /health endpoint and a placeholder /predict endpoint whose
input and output are validated/serialized by Pydantic models.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app_assignment_1 import __version__

app = FastAPI(
    title="app-assignment-1",
    version=__version__,
    description="DSAN 6700 App Deployment — Assignment 1 service skeleton.",
)


class HealthResponse(BaseModel):
    """Small typed payload confirming the service is up."""

    status: str = "ok"
    version: str = __version__


class PredictRequest(BaseModel):
    """Validated input for the placeholder predict endpoint."""

    text: str = Field(..., min_length=1, description="Input text to echo back.")


class PredictResponse(BaseModel):
    """Predictable, serialized output for the placeholder predict endpoint."""

    prediction: str
    input_length: int


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check for callers, containers, and graders."""
    return HealthResponse()


@app.post("/predict", response_model=PredictResponse)
def predict(request: PredictRequest) -> PredictResponse:
    """Placeholder predict endpoint (does no real inference yet)."""
    return PredictResponse(
        prediction=f"echo: {request.text}",
        input_length=len(request.text),
    )
