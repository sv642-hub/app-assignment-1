"""Build data/banking77_split.csv from Banking77 at a pinned commit.

Usage, from the repo root:
    uv run python scripts/make_banking77_split.py          # write the split
    uv run python scripts/make_banking77_split.py --check  # fail if it differs

Downloads Banking77's train.csv and test.csv (about 1 MB) and checks their
SHA-256 before use. Banking77 is by PolyAI, licensed CC BY 4.0.
"""

import argparse
import csv
import hashlib
import io
import sys
from collections import Counter
from pathlib import Path

import httpx

from app_assignment_1.banking77 import Example, make_split
from app_assignment_1.labels import load_labels

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "banking77_split.csv"
COMMIT = "57ec275d8078af65b7731c2a98be812d844a6d6b"
URL = (
    "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/"
    f"{COMMIT}/banking_data/{{name}}.csv"
)
SHA256 = {
    "train": "b06e26ac675513959a63135f11b94ea7786ed02da65db93a5650d8838cbc664b",
    "test": "d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d",
}


def fetch(name: str) -> list[dict[str, str]]:
    response = httpx.get(URL.format(name=name), timeout=30, follow_redirects=True)
    body = response.raise_for_status().content
    if hashlib.sha256(body).hexdigest() != SHA256[name]:
        sys.exit(f"{name}.csv does not match the pinned checksum")
    return list(csv.DictReader(io.StringIO(body.decode("utf-8"))))


def render(examples: list[Example]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(Example._fields)
    writer.writerows(examples)
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if out of date")
    args = parser.parse_args()

    labels = load_labels(ROOT / "config" / "labels.json")
    intent_for = {i.banking77_name: i.label for i in labels.items if i.banking77_name}
    train, test = fetch("train"), fetch("test")
    examples = make_split(
        [(row["text"], row["category"]) for row in train],
        [row["text"] for row in test],
        intent_for,
    )
    text = render(examples)

    if args.check:
        if TARGET.read_text(encoding="utf-8") != text:
            sys.exit(f"{TARGET.relative_to(ROOT)} is out of date; rerun the script")
        print("Split is up to date.")
        return
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    counts = Counter(example.split for example in examples)
    dropped = len(train) - len(examples)
    print(f"Wrote {counts['train']} train and {counts['dev']} dev; dropped {dropped}")


if __name__ == "__main__":
    main()
