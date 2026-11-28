"""Smart AI Gateway — DSAN 6700 mocked prototype."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The one place the version lives is `version` in pyproject.toml.
    __version__ = version("app-assignment-1")
except PackageNotFoundError:  # pragma: no cover - source tree that is not installed
    __version__ = "0+unknown"
