"""app_assignment_1 — DSAN 6700 Assignment 1 package."""

__version__ = "0.1.0"


def greeting(name: str = "world") -> str:
    """Return a friendly greeting."""
    return f"Hello, {name}!"


def main() -> None:
    """Console-script entry point."""
    print(greeting("from app-assignment-1"))
