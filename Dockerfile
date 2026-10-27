# One image definition, two targets: `gateway` (FastAPI) and `frontend`
# (Streamlit). compose.yml builds each with `target:`.
FROM python:3.13-slim AS base

RUN groupadd -r -g 10001 appgroup && \
    useradd -r -u 10001 -g appgroup -s /sbin/nologin -d /nonexistent appuser

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock ./

ENV PATH="/app/.venv/bin/:$PATH"


FROM base AS gateway

RUN uv sync --frozen --no-install-project

COPY src ./src
COPY README.md ./
COPY config ./config

RUN uv sync --frozen

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=4)"]

CMD ["uvicorn", "smart_gateway.api:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS frontend

RUN uv sync --frozen --no-install-project --extra playground

COPY src ./src
COPY README.md ./

RUN uv sync --frozen --extra playground

USER appuser

# Streamlit writes a little state under $HOME; appuser has no home directory.
ENV HOME=/tmp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"]

CMD ["streamlit", "run", "src/smart_gateway/playground.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false", "--client.toolbarMode=minimal"]
