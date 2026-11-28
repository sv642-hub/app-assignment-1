"""No blocking call inside an `async def`, and /health stays up under load."""

import ast
import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings

SRC = Path("src/app_assignment_1")
# Calls that stop the event loop while they wait.
BLOCKING_CALLS = {
    "time.sleep",
    "requests.get",
    "requests.post",
    "requests.request",
    "urllib.request.urlopen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_output",
    "os.system",
    "socket.create_connection",
}
BLOCKING_NAMES = {"open", "input", "print"}
BLOCKING_METHODS = {"read_text", "read_bytes", "write_text", "write_bytes"}
# Modules whose async code only runs at startup or offline, not per request.
OFFLINE = {"probe.py", "probe_modellens.py", "evaluation.py", "banking77.py"}


def dotted(node: ast.AST) -> str:
    """`a.b.c` for an attribute chain, else the bare name or ''."""
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def blocking_calls(path: Path) -> list[str]:
    """Blocking calls made directly inside an async function of `path`."""
    found = []
    for func in ast.walk(ast.parse(path.read_text())):
        if not isinstance(func, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = dotted(node.func)
            method = name.rsplit(".", 1)[-1]
            if (
                name in BLOCKING_CALLS
                or name in BLOCKING_NAMES
                or (isinstance(node.func, ast.Attribute) and method in BLOCKING_METHODS)
            ):
                found.append(f"{path.name}:{node.lineno} {func.name} calls {name}")
    return found


def test_no_async_function_makes_a_blocking_call() -> None:
    found = [
        line
        for path in sorted(SRC.glob("*.py"))
        if path.name not in OFFLINE
        for line in blocking_calls(path)
    ]
    # Startup reads of small config files, before the first request, are the
    # only allowed ones: they sit in `__init__` or the lifespan, not a route.
    assert found == [], found


def test_the_audit_finds_a_blocking_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import time\nasync def f():\n    time.sleep(1)\n")
    assert blocking_calls(bad) == ["bad.py:3 f calls time.sleep"]


def test_no_print_in_the_package() -> None:
    offenders = []
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and dotted(node.func) == "print":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_health_answers_while_a_slow_request_is_in_flight(tmp_path: Path) -> None:
    backends = json.loads(Path("config/backends.json").read_text())
    for backend in backends:
        backend["latency_ms"] = 1500
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(backends))
    settings = Settings(_env_file=None, backends_path=path, rate_limit_per_minute=0)
    with TestClient(create_app(settings)) as client:
        slow = threading.Thread(
            target=lambda: client.post("/v1/chat", json={"message": "card arrival"})
        )
        started = time.perf_counter()
        slow.start()
        time.sleep(0.2)
        health_started = time.perf_counter()
        response = client.get("/v1/health")
        health_took = time.perf_counter() - health_started
        slow.join()
        slow_took = time.perf_counter() - started
    assert response.status_code == 200
    assert slow_took >= 1.4
    assert health_took < 0.5
