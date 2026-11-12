import argparse
import asyncio
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from app_assignment_1.labels import load_labels
from app_assignment_1.schemas import BackendConfig, PolicyCard
from app_assignment_1.service import check_reply
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
    parser.add_argument(
        "--simulate",
        choices=("ok", "unavailable", "slow", "invalid_json"),
        help="Select a local stub response. Failure modes exit with an error.",
    )
    args = parser.parse_args()
    base_url = httpx.URL(args.base_url)
    if base_url.query:
        parser.error("Pass the stub mode with --simulate instead of a URL query.")

    labels = load_labels(CONFIG_DIR / "labels.json")
    cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
        (CONFIG_DIR / "policy_cards.json").read_text()
    )
    configs = TypeAdapter(list[BackendConfig]).validate_json(
        (CONFIG_DIR / "backends.json").read_text()
    )
    config = next(config for config in configs if config.route == args.route)

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            params={"simulate": args.simulate} if args.simulate else None,
            timeout=0.5 if args.simulate == "slow" else 60.0,
        ) as client:
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

    if reply.intent != "card_arrival":
        raise SystemExit(f"{args.route}: expected card_arrival, got {reply.intent!r}")
    errors = check_reply(reply.reply)
    if errors:
        raise SystemExit(f"{args.route}: invalid reply: {', '.join(errors)}")
    if any(fact not in cards[reply.intent].facts for fact in reply.policy_facts):
        raise SystemExit(f"{args.route}: reply listed a fact outside the policy card")

    print(f"{args.route}: {args.model} returned a valid BackendReply.")
    print(reply.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
