"""Estimate how often the probe gate passes when there is nothing to find.

Each run draws independent uniform scores for the probe and for token
confidence, so neither carries any signal about the label. A run is a false
pass if both interval conditions of the pass rule in docs/PROBE.md hold: the
interval on AUROC(probe) - AUROC(token) and the interval on AUROC(probe) - 0.5
both stay above zero. If the intervals behave, this should happen in at most
about 2.5% of runs.

`--condition difference` or `--condition chance` checks one interval alone.
`chance` is the worst case for the full rule: if token confidence ranked
below chance, the difference interval would clear zero almost always.

Usage:
    uv run python scripts/probe_false_pass.py --correct 20 --wrong 20 --runs 2000

This is a simulation of the gate's statistics, not a result on any model.
"""

import argparse
import math
import random
from concurrent.futures import ProcessPoolExecutor

from app_assignment_1.probe import CHANCE_AUROC, auroc_difference_interval

CONDITIONS = ("both", "difference", "chance")


def false_pass(seed: int, correct: int, wrong: int, condition: str) -> bool:
    rng = random.Random(seed)
    labels = [True] * correct + [False] * wrong
    probe = [rng.random() for _ in labels]
    token = [rng.random() for _ in labels]
    if condition != "chance":
        difference_low, _ = auroc_difference_interval(probe, token, labels)
        if difference_low <= 0 or condition == "difference":
            return difference_low > 0
    chance_low, _ = auroc_difference_interval(
        probe, [CHANCE_AUROC] * len(labels), labels
    )
    return chance_low > 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--correct", type=int, default=20)
    parser.add_argument("--wrong", type=int, default=20)
    parser.add_argument("--runs", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0, help="first run's seed")
    parser.add_argument("--condition", choices=CONDITIONS, default="both")
    args = parser.parse_args()

    seeds = range(args.seed, args.seed + args.runs)
    with ProcessPoolExecutor() as pool:
        results = list(
            pool.map(
                false_pass,
                seeds,
                [args.correct] * args.runs,
                [args.wrong] * args.runs,
                [args.condition] * args.runs,
                chunksize=50,
            )
        )

    passes = sum(results)
    rate = passes / args.runs
    error = math.sqrt(rate * (1 - rate) / args.runs)
    print(
        f"{args.correct} correct, {args.wrong} wrong, condition "
        f"{args.condition}: {passes} of {args.runs} "
        f"runs passed ({rate:.2%}, standard error {error:.2%}), seeds "
        f"{args.seed} to {args.seed + args.runs - 1}"
    )


if __name__ == "__main__":
    main()
