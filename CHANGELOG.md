# Changelog

All notable changes to this project are documented here, most recent at top.

## [0.1.0] - 2026-09-06

### Added
- Initial project scaffold with `src/` layout (`src/app_assignment_1/`).
- `pyproject.toml` with hatchling build backend, single `dev` extra, and ruff/pytest config.
- Reproducible shared environment via committed `uv.lock`.
- FastAPI service (`api.py`): typed `GET /health` and placeholder `POST /predict`,
  with Pydantic request/response models validating at the edge.
- Typed configuration via Pydantic Settings (`config.py`) with fail-fast startup
  validation and paired `.env.example` / `.env.production.example` templates.
- GitHub Actions CI (`.github/workflows/ci.yml`): ruff check, ruff format check,
  mypy, and pytest on every push and pull request.
- Diátaxis-structured README and function docstrings.
- API test suite (`tests/test_api.py`) covering `/health` and `/predict`.
- Pre-commit hooks for ruff lint/format.

### Removed
- `uv init` hello-world placeholder (`greeting`/`main` and console script) and its
  test, replaced by real API tests.
