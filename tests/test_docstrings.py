"""Every module, class, function and method in src/ and scripts/ has a docstring."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _missing(path: Path) -> list[str]:
    """Return the names in one file that have no docstring."""
    tree = ast.parse(path.read_text())
    missing = [] if ast.get_docstring(tree) else ["<module>"]
    for node in ast.walk(tree):
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if isinstance(node, kinds) and not ast.get_docstring(node):
            missing.append(f"{node.name} (line {node.lineno})")
    return missing


def test_every_definition_has_a_docstring():
    """Fail with the list of definitions that lack a docstring."""
    problems = []
    for folder in ("src", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            problems += [f"{path.relative_to(ROOT)}: {m}" for m in _missing(path)]
    assert not problems, "missing docstrings:\n" + "\n".join(problems)
