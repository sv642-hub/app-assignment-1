# Linear probe

The probe reads Qwen3-1.7B activations and estimates whether the model's output is correct. Under the proposed label (see "Proposed decisions"), that means whether it classified the message's intent correctly, and reply quality would stay with the format checks and the judge. The probe has to beat the model's own token-probability confidence on the dev set (see "Probe gate" in `docs/TEAM_PLAN.md`). Until it does, it stays out of the live system.

**Status: skeleton only.** `src/app_assignment_1/probe.py` runs end to end on mock activations. No real model has been probed, so there are no results yet. The gateway does not import this module.

## How it fits together

1. `ProbeExample` holds a prompt, a reply, a correct/wrong label and the token confidence.
2. An `ActivationExtractor` turns each prompt and reply into one vector. `MockActivationExtractor` uses hashes, so it carries no signal.
3. `train_probe` fits a logistic regression, giving a `LinearProbe`.
4. `run_gate` compares both scores on the dev set with `auroc`. It returns a `GateReport` that includes a `GateRunInfo`: the model, layer, pooling, token confidence method and dataset version.

## Pass rule

The probe passes only if both of these hold:

- The activations are real, so a mock run can never pass. `mocked` is true for any report built on mock activations.
- The 95% interval on the AUROC difference (probe minus token confidence) stays above zero. `auroc_difference_interval` gets this from a paired bootstrap: 2,000 resamples with a fixed seed, using the same examples for both scores and resampling each class on its own.

`run_gate` raises an error if a prompt appears in both sets, ignoring case and whitespace, but it does not catch paraphrases. `train_probe` raises an error if the training set has only one class.

`run_gate` also raises an error if the dev set has fewer than 20 correct or 20 wrong examples. In synthetic runs with no signal, the interval passed far too often on small or lopsided dev sets: 10.0% of runs with 100 correct and 3 wrong, and 3.0% with 100 and 20, against about 2.5% if it behaves. The number 20 comes from those runs, not from a published threshold.

## Still to build

- A real `ActivationExtractor` for Qwen3-1.7B, run offline. Torch and Transformers should go in a separate optional extra, so the gateway image stays small.
- A way to collect token confidence during the same offline pass.
- A development split and loader. Banking77 has only train and test, so this has to be carved from its training data. The frozen evaluation set stays out of probe training and the gate, and is kept for one final report. The human grades are too few to train on, and are for checking the judge.
- A faster trainer with regularization, such as scikit-learn, to replace `train_probe`.
- A script that runs the gate and saves the report, including the controls, routing analysis and escalation curve below.

## Proposed decisions

These are proposals for team review, fixed before any real run. Nothing here has been measured on Qwen3-1.7B.

- **Label:** "correct" means the predicted intent equals the gold intent. Banking77 has no gold `unknown`, so a prediction of `unknown` is always wrong on this data; report results with and without those cases. An automated study estimated that up to 14% of the training labels may be wrong (Ying and Thomas, 2022), so check a sample of the wrong cases by hand.
- **Activations:** the last intent-label token is primary, and the end of the question is secondary, saved in the same pass. The layer is chosen by cross-validation on training data, never on dev. A live version would be cheap only if `intent` comes first in the JSON, which needs checking with Yanzhen.
- **Token confidence:** fix a short list of candidates in advance: mean log probability and minimum probability over the label tokens, joint label probability, and mean over all output tokens. Choose the strongest on training data and use only that one on dev. Record whether it uses raw or constrained probabilities. `token_confidence` must be between 0 and 1, so a mean log probability is exponentiated first, which does not change AUROC.
- **Controls:** the report adds a text-only classifier, a per-intent accuracy prior and AUROC within each intent. They do not change the pass rule. They decide what a pass may claim, because a text-only classifier can match a probe on in-distribution data (Moreno Cencerrado et al., 2025).
- **Routing analysis:** alongside the gate, also score the cases where the small model is wrong and the large model is right, because escalating only helps there. This needs Qwen3-4B outputs on the same dev data. It does not change the pass rule.
- **Escalation curve:** report the share of wrong outputs caught at each escalation rate, alongside AUROC. AUROC shows the ranking is good, but not what a threshold would do in a live system.
- **Data split:** Jackson owns this. Use one fixed, seeded split, stratified by intent. The human grades draw from the same data, so agree it once with Sebastian and Yanzhen.

## Still open

- **Gate date:** `[PROBE GATE DATE]` in `docs/TEAM_PLAN.md` still needs a real date.
