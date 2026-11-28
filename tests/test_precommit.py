"""The pre-commit ruff hook runs the same ruff version as CI (uv.lock)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_precommit_ruff_matches_the_locked_ruff():
    config = (ROOT / ".pre-commit-config.yaml").read_text()
    match = re.search(r"ruff-pre-commit\s+rev:\s*v(\S+)", config)
    assert match, "no ruff-pre-commit rev in .pre-commit-config.yaml"
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = [p["version"] for p in lock["package"] if p["name"] == "ruff"]
    assert locked == [match.group(1)], (
        f"pre-commit runs ruff {match.group(1)}, uv.lock has {locked}: "
        "set the pre-commit rev to the locked version"
    )
