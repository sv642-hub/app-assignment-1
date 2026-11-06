"""docs/architecture.svg must match docs/architecture.yaml, and bad YAML is caught."""

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "render_architecture.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_architecture", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arch = load_script()


def small_doc() -> dict[str, Any]:
    return {
        "title": "t",
        "subtitle": "s",
        "components": {
            "box": {"title": "Box", "owner": None, "items": [{"built": "Works"}]}
        },
        "layout": {"size": [400, 300], "boxes": {"box": [10, 10, 300, 120]}},
    }


def test_committed_svg_matches_the_yaml() -> None:
    expected = arch.render(arch.load())
    assert arch.TARGET.read_text(encoding="utf-8") == expected, (
        "docs/architecture.svg is out of date. Run: "
        "uv run python scripts/render_architecture.py"
    )


def test_check_mode_passes_on_the_committed_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert arch.main(["--check"]) == 0
    assert "matches" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kinds", "status"),
    [
        (["built", "built"], "built"),
        (["built", "todo"], "partial"),
        (["built", "simulated"], "partial"),
        (["simulated", "todo"], "todo"),
        (["todo"], "todo"),
    ],
)
def test_box_status_comes_from_items(kinds: list[str], status: str) -> None:
    assert arch.box_status([{kind: "x"} for kind in kinds]) == status


def test_unowned_box_says_so() -> None:
    assert "Owner: not assigned" in arch.render(small_doc())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["components"]["box"]["items"].append({"done": "x"}), "built"),
        (lambda d: d["layout"]["boxes"].pop("box"), "no position for `box`"),
        (lambda d: d["layout"]["boxes"].update(extra=[0, 0, 1, 1]), "not a component"),
        (lambda d: d["components"]["box"].update(colour="red"), "unknown key"),
        (lambda d: d["components"]["box"].pop("owner"), "missing `owner`"),
        (
            lambda d: d["layout"].update(arrows=[{"kind": "fast", "points": [[0, 0]]}]),
            "kind",
        ),
    ],
)
def test_bad_yaml_is_explained(change: Any, message: str) -> None:
    doc = copy.deepcopy(small_doc())
    change(doc)
    with pytest.raises(arch.ArchitectureError, match=message):
        arch.render(doc)


def test_overflowing_box_is_an_error() -> None:
    doc = small_doc()
    doc["components"]["box"]["items"] = [{"todo": "A long line of text"}] * 8
    with pytest.raises(arch.ArchitectureError, match="make the box taller"):
        arch.render(doc)


def test_text_is_escaped() -> None:
    doc = small_doc()
    doc["components"]["box"]["items"] = [{"built": "<b> & co"}]
    assert "&lt;b&gt; &amp; co" in arch.render(doc)
