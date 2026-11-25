"""Export requests and Jev assessments from Postgres for analysis.

Writes two JSON Lines files that join on the request ID:

- requests.jsonl: one line per request, with its route, model, intent, format
  check, settings version and trace. The reply text and policy facts are left
  out; nothing here ever held the customer's message.
- jev_assessments.jsonl: one line per Jev answer (two per request), with the
  choice, confidence, probabilities, the order the route options were shown in,
  tokens and errors. Assessments whose request was deleted under an old
  retention limit are still exported.

Usage, from the repo root, with the database URL in the environment so it never
appears in shell history or the process list:

    APP_DATABASE_URL=postgresql://... uv run python scripts/export_analysis.py \\
        --out exports/2026-11-22
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

# The reply text and policy facts are left out: statistics don't need them.
REQUEST_FIELDS = ("id", "completed_at", "intent")


def request_row(response: dict[str, Any], settings_version: str | None) -> dict:
    row = {key: response[key] for key in REQUEST_FIELDS}
    row["settings_version"] = settings_version
    row["trace"] = response["trace"]
    return row


def export(url: str, out: Path) -> tuple[int, int]:
    """Write both files to `out` and return how many lines each has."""
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(url) as conn:
        requests = 0
        with (out / "requests.jsonl").open("w", encoding="utf-8") as f:
            cur = conn.execute(
                "SELECT response, settings_version FROM requests ORDER BY seq"
            )
            for response, version in cur:
                f.write(json.dumps(request_row(response, version)) + "\n")
                requests += 1
        assessments = 0
        with (out / "jev_assessments.jsonl").open("w", encoding="utf-8") as f:
            cur = conn.execute("SELECT record FROM jev_assessments ORDER BY seq")
            for (record,) in cur:
                f.write(json.dumps(record) + "\n")
                assessments += 1
    return requests, assessments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="output folder")
    args = parser.parse_args(argv)
    url = os.environ.get("APP_DATABASE_URL")
    if not url:
        print("Set APP_DATABASE_URL to the Postgres URL.", file=sys.stderr)
        return 2
    try:
        requests, assessments = export(url, args.out)
    except psycopg.Error as exc:
        # The driver's message can quote the URL, password included.
        print(f"Export failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"Wrote {requests} requests and {assessments} Jev assessments to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
