FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
WORKDIR /app
RUN groupadd -r -g 10001 appgroup && useradd -r -u 10001 -g appgroup -d /home/appuser -m appuser
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY README.md ./
RUN uv sync --frozen --no-dev
USER appuser
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=4)"]
CMD ["uvicorn", "app_assignment_1.api:app", "--host", "0.0.0.0", "--port", "8000"]
