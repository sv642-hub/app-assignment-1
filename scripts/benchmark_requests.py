"""Measure ten concurrent requests after one warm-up request."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

URL = "http://127.0.0.1:8000/predict"
PAYLOAD = {"text": "What is a bank? Answer in one short sentence."}
REQUEST_COUNT = 10


async def main(label: str) -> None:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(300.0),
        limits=httpx.Limits(max_connections=REQUEST_COUNT),
        trust_env=False,
    ) as client:
        print("Warming up the model...")
        warmup = await client.post(URL, json=PAYLOAD)
        warmup.raise_for_status()
        print("Warm-up complete. Sending 10 concurrent requests...")

        async def send_request(number: int) -> dict:
            start = perf_counter()
            response = await client.post(URL, json=PAYLOAD)
            elapsed = perf_counter() - start
            response.raise_for_status()
            body = response.json()

            if not isinstance(body.get("prediction"), str):
                raise ValueError("Response is missing a text prediction.")

            return {
                "request": number,
                "seconds": elapsed,
                "status_code": response.status_code,
                "prediction": body["prediction"],
            }

        batch_start = perf_counter()
        results = await asyncio.gather(
            *(send_request(i + 1) for i in range(REQUEST_COUNT))
        )
        total = perf_counter() - batch_start

    slowest = max(result["seconds"] for result in results)
    report = {
        "label": label,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "url": URL,
        "payload": PAYLOAD,
        "request_count": REQUEST_COUNT,
        "warmup_requests": 1,
        "total_seconds": total,
        "slowest_request_seconds": slowest,
        "requests": results,
    }

    output = Path("benchmark_results") / f"{label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for result in results:
        print(f"Request {result['request']:2}: {result['seconds']:.3f} s")

    print(f"\nTotal wall-clock time: {total:.3f} s")
    print(f"Slowest request:      {slowest:.3f} s")
    print(f"Results saved to: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=["blocking", "fixed"])
    args = parser.parse_args()
    asyncio.run(main(args.label))
