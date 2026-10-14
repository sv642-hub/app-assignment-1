FROM python:3.13-slim

RUN groupadd -r -g 10001 appgroup && \
    useradd -r -u 10001 -g appgroup -s /sbin/nologin -d /nonexistent appuser

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
WORKDIR /app

RUN mkdir -p /models && chown appuser:appgroup /models

ENV HF_HOME=/models

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project

COPY src ./src
COPY README.md ./

RUN uv sync --frozen

USER appuser

ENV PATH="/app/.venv/bin/:$PATH"

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "app_assignment_1.api:app" ,"--host", "0.0.0.0", "--port", "8000"]