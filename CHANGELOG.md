# Changelog

All notable changes for users and operators of the Smart AI Gateway. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [semantic versioning](https://semver.org/). The version lives only in
`pyproject.toml`; each release is a Git tag `vX.Y.Z` on `main`.

## [Unreleased]

### Added
- Optional second Jev call that checks each reply, off by default (#133).
- Escalation to Qwen3-4B when the small model's intent confidence is low, off by default (#134).
- Real vLLM backends for Qwen3-1.7B and Qwen3-4B, with typed failures (#117).
- Per-model releases, rollback, and automatic rollback of a bad release (#107).
- Operator routes and fault-injection routes for the chaos scenarios (#95, #115, #119).
- Prometheus alert rules, a metrics catalog and a monitoring runbook (#101, #102, #145).
- Controller and monitor state restored from Postgres at startup (#100).
- Hung-model detection in about 18 s, with time to detect recorded (#135).
- Start-at-boot systemd units for the CPU and GPU instances (#113).

### Changed
- Dev tools moved from an optional extra to a `dev` dependency group:
  install with `uv sync --frozen`.

## [0.2.0]

Mocked midpoint prototype: FastAPI gateway, mock Jev router and Qwen
backends, React chat and Health screens, Postgres store, Caddy proxy, and the
first monitor and controller.

[Unreleased]: https://github.com/sv642-hub/app-assignment-1/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sv642-hub/app-assignment-1/releases/tag/v0.2.0
