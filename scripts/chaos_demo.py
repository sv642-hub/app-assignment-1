"""Live chaos demo: python scripts/chaos_demo.py [base_url] [--scenario NAME]
[--prometheus].

Start the stack first with `compose.demo.yml` (see docs/CHAOS_DEMO.md). The
script injects each fault into the small model through POST /v1/faults, sends
messages until the controller disables the model, clears the fault, and waits
for the controller to bring the model back. It prints what happened and the
times it measured in this run. It sends only the fixed example messages below
and never prints or stores anything else.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8080"
# Public throwaway value; it matches compose.demo.yml and protects nothing.
DEMO_TOKEN = "chaos-demo-token-not-for-deployment"
SMALL, LARGE = "qwen3-1.7b", "qwen3-4b"
STANDARD = "My card has not arrived"
COMPLEX = "My card was stolen"
MAX_MESSAGES = 30
SCRAPE_SECONDS = 15  # prometheus/prometheus.yml scrapes every 15 seconds

# name: (fault sent to /v1/faults, what the presenter should say)
SCENARIOS = {
    "bad_format": ("bad_format", "The small model corrupts every reply."),
    "outage": ("outage", "Every call to the small model raises an error."),
    "label_swap": (
        "label_swap",
        "The small model answers well-formed replies for the wrong intent.",
    ),
}

DISABLED_QUERY = "gateway_model_disabled"
QUERIES = [
    DISABLED_QUERY,
    "gateway_monitor_failure_rate",
    "gateway_semantic_disagreement_rate",
    # The demo's own reset adds operator_* rows; leave them out.
    "sum by (action, model) (gateway_controller_interventions_total"
    '{action=~"disabled|re-enabled"})',
]


class DemoError(Exception):
    """The stack did not behave the way the demo expects."""


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def call(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if payload is not None and path != "/v1/chat":
            headers["Authorization"] = "Bearer " + self.token
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except HTTPError as error:
            raise DemoError(f"{path} answered {error.code}") from None
        except (URLError, TimeoutError, ConnectionError) as error:
            raise DemoError(f"cannot reach {self.base}{path}: {error}") from None
        except ValueError:
            raise DemoError(f"{path} did not return valid JSON") from None


def monitor(api: Any, name: str) -> dict[str, Any]:
    health = api.call("/v1/health")
    return {m["name"]: m for m in health["monitor"]}[name]


def send(api: Any, message: str) -> dict[str, Any]:
    trace = api.call("/v1/chat", {"message": message})["trace"]
    return {
        "model": trace["model"],
        "format": trace["format_check"],
        "fallback": trace["fallback_reason"],
        "action": trace["controller_action"],
    }


def show(step: int, kind: str, reply: dict[str, Any], out: Callable[[str], None]):
    line = f"  {step:>2}. {kind:<8} -> {reply['model']:<10} format {reply['format']}"
    if reply["fallback"]:
        line += f" | {reply['fallback']}"
    if reply["action"]:
        line += f" | CONTROLLER: {reply['action']}"
    out(line)


def reset(api: Any) -> None:
    """Clear every fault, lift any disable, and start the small model's failure
    and semantic windows again.

    Enabling a model that is not disabled changes nothing, so the small model is
    held and released: releasing a hold always starts its windows again. Without
    that, replies from the previous scenario stay in its windows and the
    controller's reason reads, say, "80% over last 5 calls" after 4 messages.
    """
    for model in (SMALL, LARGE):
        api.call("/v1/faults", {"model": model, "fault": "none"})
        api.call(f"/v1/operator/models/{model}/enable", {"reason": "demo reset"})
    api.call(f"/v1/operator/models/{SMALL}/disable", {"reason": "demo reset"})
    api.call(f"/v1/operator/models/{SMALL}/enable", {"reason": "demo reset"})


def clear_faults(api: Any) -> bool:
    """Try every cleanup action even if an earlier call fails."""
    clean = True
    for model in (SMALL, LARGE):
        for path, payload in (
            ("/v1/faults", {"model": model, "fault": "none"}),
            (f"/v1/operator/models/{model}/enable", {"reason": "demo cleanup"}),
        ):
            try:
                api.call(path, payload)
            except DemoError as error:
                clean = False
                print(f"WARNING: cleanup failed for {model}: {error}", file=sys.stderr)
    if not clean:
        print(
            "DEMO FAILED: cleanup incomplete. See docs/CHAOS_DEMO.md.",
            file=sys.stderr,
        )
    return clean


def run_scenario(
    api: Any,
    name: str,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    while_disabled: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run one scenario. `while_disabled`, if given, runs after the fault is
    cleared and before the cooldown ends, with the seconds left until it ends,
    so it can show the model while it is still disabled."""
    fault, story = SCENARIOS[name]
    out(f"\n=== {name}: {story}")
    reset(api)
    rules = api.call("/v1/health")["controller"]
    step = 0
    if name == "label_swap":
        # The controller only disables a model on its intents while the other
        # model has answered recently, so the large model answers once first.
        step += 1
        show(step, "complex", send(api, COMPLEX), out)

    api.call("/v1/faults", {"model": SMALL, "fault": fault, "rate": 1})
    injected = clock()
    out(f"  fault '{fault}' injected on {SMALL}")
    sent = 0
    while not monitor(api, SMALL)["disabled_by_controller"]:
        if sent >= MAX_MESSAGES:
            raise DemoError(f"{SMALL} was not disabled after {sent} messages")
        step, sent = step + 1, sent + 1
        show(step, "standard", send(api, STANDARD), out)
    disabled_at = clock()
    detected = disabled_at - injected
    state = monitor(api, SMALL)
    out(f"  {SMALL} disabled for {state['disabled_for']} after {sent} messages")

    step += 1
    after = send(api, STANDARD)
    show(step, "standard", after, out)
    if after["model"] != LARGE or after["format"] != "passed":
        raise DemoError(f"traffic did not move to {LARGE} cleanly: {after}")

    # Clear the fault first, so the model is healthy if a check re-enables it
    # while `while_disabled` runs.
    api.call("/v1/faults", {"model": SMALL, "fault": "none"})
    cooldown = rules["cooldown_seconds"]
    out(f"  fault cleared; waiting for the {cooldown:g} s cooldown")
    if while_disabled is not None:
        while_disabled(cooldown - (clock() - disabled_at))
    while True:
        if clock() - disabled_at > cooldown + 30:
            raise DemoError(f"{SMALL} did not come back after the cooldown")
        sleep(2)
        step += 1
        reply = send(api, STANDARD)
        show(step, "standard", reply, out)
        if reply["model"] == SMALL and reply["format"] == "passed":
            break
    recovered = clock() - disabled_at
    return {
        "scenario": name,
        "messages_to_disable": sent,
        "seconds_to_disable": detected,
        "seconds_to_recover": recovered,
    }


def query_prometheus(query: str) -> list[dict[str, Any]]:
    """Run one instant query inside the prometheus container (no port is
    published). Raise DemoError if Prometheus cannot be reached."""
    command = ["docker", "compose", "exec", "-T", "prometheus", "wget", "-qO-"]
    url = "http://127.0.0.1:9090/api/v1/query?query=" + quote(query)
    done = subprocess.run(command + [url], capture_output=True, text=True)
    if done.returncode:
        raise DemoError("could not reach Prometheus: " + done.stderr.strip()[:200])
    rows: list[dict[str, Any]] = json.loads(done.stdout)["data"]["result"]
    return rows


# When Prometheus last scraped the gateway, in Prometheus's own clock.
SCRAPED_QUERY = f'timestamp(gateway_model_disabled{{model="{SMALL}"}})'


def scraped_at(rows: list[dict[str, Any]]) -> str | None:
    return rows[0]["value"][1] if rows else None


def prometheus(
    seconds_left: float,
    out: Callable[[str], None] = print,
    run: Callable[[str], list[dict[str, Any]]] = query_prometheus,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Show Prometheus while the small model is still disabled. The model is
    already disabled when this runs, so the first scrape after it starts sees
    the disable; an earlier one may still show the previous scenario."""
    out(f"\nPrometheus, while {SMALL} is disabled (scraped every {SCRAPE_SECONDS} s)")
    deadline = clock() + max(seconds_left - 1, 0)
    try:
        before = scraped_at(run(SCRAPED_QUERY))
        while scraped_at(run(SCRAPED_QUERY)) in (before, None):
            if clock() >= deadline:
                out("  no new scrape before the cooldown ended; values may be old")
                break
            sleep(1)
        for query in QUERIES:
            out(f"  {query}")
            for row in run(query):
                labels = ",".join(
                    f"{k}={v}" for k, v in row["metric"].items() if k != "__name__"
                )
                out(f"    {{{labels}}} {row['value'][1]}")
    except DemoError as error:
        out("  " + str(error))
    out("")


def summary(results: list[dict[str, Any]], out: Callable[[str], None] = print) -> None:
    out("\nMeasured in this run (one run each; not an average)")
    out("  to recover: from the disable until the small model answers again")
    out(f"  {'scenario':<12} {'messages':>8} {'to disable':>11} {'to recover':>11}")
    for r in results:
        out(
            f"  {r['scenario']:<12} {r['messages_to_disable']:>8} "
            f"{r['seconds_to_disable']:>9.1f} s {r['seconds_to_recover']:>9.1f} s"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument(
        "--prometheus", action="store_true", help="query while each model is disabled"
    )
    args = parser.parse_args(argv)
    api = Api(args.url, os.environ.get("APP_OPERATOR_TOKEN", DEMO_TOKEN))
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    exit_code = 0
    try:
        for name in names:
            hook = prometheus if args.prometheus else None
            results.append(run_scenario(api, name, while_disabled=hook))
    except DemoError as error:
        print("DEMO FAILED: " + str(error), file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("DEMO FAILED: interrupted", file=sys.stderr)
        exit_code = 130
    finally:
        if not clear_faults(api):
            exit_code = exit_code or 1
    if exit_code:
        return exit_code
    summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
