# Linear probe

The probe reads Qwen3-1.7B activations and estimates whether a reply is correct. It has to beat the model's own token-probability confidence on the dev set (see "Probe gate" in `docs/TEAM_PLAN.md`). Until it does, it stays out of the live system.

**Status: skeleton only.** `src/app_assignment_1/probe.py` runs end to end on mock activations. No real model has been probed, so there are no results yet. The gateway does not import this module.

## How it fits together

1. `ProbeExample` holds a prompt, a reply, a correct/wrong label and the token confidence.
2. An `ActivationExtractor` turns each prompt and reply into one vector. `MockActivationExtractor` uses hashes, so it carries no signal.
3. `train_probe` fits a logistic regression, giving a `LinearProbe`.
4. `run_gate` compares both scores on the dev set with `auroc`. It returns a `GateReport`, and `passed` is true only if the probe's AUROC is strictly higher and the activations are real, so a mock run can never pass. `mocked` is true for any report built on mock activations. `run_gate` raises an error if a prompt appears in both sets, but it does not catch paraphrases.

## Still to build

- A real `ActivationExtractor` for Qwen3-1.7B, run offline. Torch and Transformers should go in a separate optional extra, so the gateway image stays small.
- A way to collect token confidence during the same offline pass.
- A loader for the frozen evaluation set and human grades.
- A faster trainer with regularization, such as scikit-learn, to replace `train_probe`.
- A script that runs the gate and saves the report.
- A confidence interval on the AUROC difference, since the dev set is small.

## Decisions to make

- **Label:** this skeleton assumes "correct" means the reply was right, but not how that is graded. It could be the intent matching the evaluation set, or a human or judge grade.
- **Activations:** which layer and token position to use, and how to pool them.
- **Token confidence:** how to reduce per-token probabilities to one score, such as the mean log probability.
- **Pass rule:** the skeleton passes on any higher AUROC, however small. We could require a minimum margin, or a confidence interval that stays above zero.
- **Data split:** how to divide the set into train and dev without touching the frozen test data.
- **Gate date:** `[PROBE GATE DATE]` in `docs/TEAM_PLAN.md` still needs a real date.
