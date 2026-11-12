import argparse
import asyncio
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from app_assignment_1.labels import load_labels
from app_assignment_1.schemas import BackendConfig, PolicyCard
from app_assignment_1.vllm import (
    BackendCallError,
    BackendOutputError,
    BackendTimeoutError,
    VLLMBackend,
    build_system_prompt,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify one live vLLM endpoint.")
    parser.add_argument("--route", choices=("standard", "complex"), required=True)
    parser.add_argument("--base-url", required=True, help="Server URL without /v1")
    parser.add_argument("--model", required=True, help="Model name exposed by vLLM")
    args = parser.parse_args()

    labels = load_labels(CONFIG_DIR / "labels.json")
    cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
        (CONFIG_DIR / "policy_cards.json").read_text()
    )
    configs = TypeAdapter(list[BackendConfig]).validate_json(
        (CONFIG_DIR / "backends.json").read_text()
    )
    config = next(config for config in configs if config.route == args.route)

    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as client:
            backend = VLLMBackend(
                config,
                client,
                model=args.model,
                system_prompt=build_system_prompt(labels, cards),
                labels=labels,
            )
            reply = await backend.generate("When will my new bank card arrive?")
    except (BackendCallError, BackendTimeoutError, BackendOutputError) as exc:
        raise SystemExit(f"{args.route}: {type(exc).__name__}: {exc}") from None

    if reply.intent not in labels.names:
        raise SystemExit(f"{args.route}: unexpected intent {reply.intent!r}")

    print(f"{args.route}: {args.model} returned a valid BackendReply.")
    print(reply.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
