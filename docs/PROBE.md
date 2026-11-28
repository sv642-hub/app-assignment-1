# Linear probe

The probe reads Qwen3-1.7B activations and estimates whether the model's output is correct. Under the proposed label (see "Proposed decisions"), that means whether it classified the message's intent correctly, and reply quality would stay with the format checks and the judge. The probe has to beat the model's own token-probability confidence on the dev set (see "Probe gate" in `docs/TEAM_PLAN.md`). Until it does, it stays out of the live system.

**Status: skeleton, plus a real extractor that has not been run on Qwen3-1.7B.** `src/app_assignment_1/probe.py` runs end to end on mock activations, and `src/app_assignment_1/probe_modellens.py` reads real activations with ModelLens (see below). No real model has been probed, so there are no results yet. The gateway imports neither module.

## How it fits together

1. `ProbeExample` holds a prompt, a reply, a correct/wrong label and the token confidence. Its fields are strict: `correct` must be a real `True` or `False`, and `token_confidence` a number from 0 to 1, not `True` or a string. A prompt that is empty after `normalize_prompt` is rejected. `GateRunInfo` and `GateReport` are strict in the same way, and `GateRunInfo` strips its strings, so whitespace alone counts as empty.
2. An `ActivationExtractor` turns each prompt and reply into one vector. `MockActivationExtractor` uses hashes, so it carries no signal.
3. `train_probe` fits a logistic regression, giving a `LinearProbe`. `LinearProbe.score` gives a probability, and `LinearProbe.logit` gives the log odds before the sigmoid.
4. `run_gate` compares both scores on the dev set with `auroc`. It ranks the probe by logit, not probability: the sigmoid rounds to exactly 1.0 once the logit passes about 37, which would turn different outputs into ties. AUROC only uses the order, so the logit gives the same result wherever the probability doesn't round. It returns a `GateReport` with both AUROCs, the interval on their difference, the interval on the probe's own AUROC, and a `GateRunInfo`: the model, layer, pooling, token confidence method and dataset version.

## Pass rule

The probe passes only if all three of these hold (`gate_passes`):

- The activations are real, so a mock run can never pass. `mocked` is true for any report built on mock activations.
- The 95% interval on the AUROC difference (probe minus token confidence) stays above zero. `auroc_difference_interval` gets this from a paired bootstrap: 2,000 resamples with a fixed seed, using the same examples for both scores and resampling each class on its own.
- The 95% interval on the probe's own AUROC stays above 0.5 (`probe_auroc_low` > 0.5). Without this, a broken extractor that returns the same vector every time scores exactly 0.5 and would pass whenever token confidence ranks worse than chance. The interval comes from the same bootstrap, run against a constant score, whose AUROC is always 0.5.

`GateReport` checks this rule when a report is built or loaded with `model_validate_json`: it rejects a report where `passed` disagrees with the rule, or where an interval's low end is above its high end. The report is frozen, so its fields cannot be changed afterwards. `model_construct` skips all checks, so do not use it for reports.

`run_gate` compares prompts after `normalize_prompt`, so it does not depend on how the dev split was cleaned. `normalize_prompt` applies Unicode NFKC, drops invisible characters, ignores case and collapses whitespace. The invisible characters it drops are format characters (such as zero-width spaces, byte order marks and soft hyphens), control characters other than whitespace, variation selectors, the combining grapheme joiner, Hangul fillers and the blank braille pattern. This list covers known cases, not every possible one. Look-alike letters from other scripts (such as Cyrillic "о" for Latin "o") and paraphrases are not caught. After normalizing, `run_gate` raises an error if:

- a prompt appears in both sets, because it would inflate the probe's score;
- a prompt appears twice in the same set, because the bootstrap and the 20-per-class check would count the copies as independent examples. In synthetic no-signal runs reported in the review of #62 (not rerun here), one wrong prompt copied 20 times passed the old gate in 37.4% of runs.

`train_probe` raises an error if the training set has only one class. `auroc` and `auroc_difference_interval` raise an error for a score that is NaN or infinite. A NaN used to make `auroc` loop forever, because NaN never equals itself.

`run_gate` also raises an error if the dev set has fewer than 20 correct or 20 wrong examples, because the interval is overconfident on small or lopsided dev sets. The number 20 is a judgment from the simulation below, not a published threshold.

`scripts/probe_false_pass.py` measures how often the gate passes when neither score carries any signal (uniform random scores, seeds 0 onward). If an interval behaves, it should clear zero in about 2.5% of runs. These are simulations of the gate's statistics, not results on any model:

| Dev set (correct, wrong) | Condition | Runs | Passed |
|---|---|---|---|
| 20, 20 | Full rule | 4,000 | 0.95% |
| 20, 20 | Difference interval only (the old rule) | 4,000 | 3.15% |
| 20, 20 | Probe-above-chance interval only | 4,000 | 2.35% |
| 100, 20 | Full rule | 4,000 | 1.12% |
| 100, 3 | Full rule | 2,000 | 4.90% |

The standard error is about 0.2 to 0.3 points at 4,000 runs and 0.5 at 2,000. So at the floor, the difference interval alone passes slightly too often, about 3% against 2.5%, which matches the review of #62. The full rule passes less often, because both intervals must clear zero. The worst case is token confidence ranking below chance: then only the probe-above-chance interval matters, at about 2.35%. A lopsided set like 100 and 3 passes about twice as often as it should, even under the full rule, which is why the floor exists. To rerun one row: `uv run python scripts/probe_false_pass.py --correct 20 --wrong 20 --runs 4000 --condition difference`.

## Live escalation (token confidence for now)

The gateway can already escalate a message from the small model to the large one when the small model looks unsure (`confidence.py`, README "Escalation"). It is off by default. Today the score is token confidence: the joint probability of the intent label's tokens, from vLLM's logprobs. These are raw probabilities, before the JSON schema constrains the output, as vLLM returns them by default. The probe would replace that score only if it passes the gate below. The 0.5 threshold is a placeholder until the escalation curve on dev data picks one.

## Reading real activations with ModelLens

[ModelLens](https://github.com/SebastianVillalobosAlva/ModelLens) is Sebastian's interpretability toolkit. It captures a PyTorch model's layer outputs with forward hooks, and it supports Qwen3. It also has its own linear probes (`modellens.analysis.probing`: `train_probe`, `probe_sweep`, `apply_probe`), which help with exploring, for example choosing a layer with `probe_sweep`.

`src/app_assignment_1/probe_modellens.py` uses it for the real extractor, `ModelLensExtractor`. The extractor captures one Qwen3 decoder layer (`model.layers.N`) with ModelLens and pools it with the same function ModelLens's probes use: `last` keeps the final token, and `mean` averages over all tokens. It rebuilds the text the way vLLM builds it for the gateway: the gateway's system prompt (`gateway_system_prompt`, from the same labels and policy cards), the customer message, the chat template's generation prompt, and then the reply. Nothing follows the reply, so `last` lands on the reply's final token, today the closing `}` of the JSON, not on the template's closing tokens. Pass the reply exactly as the model generated it (the raw completion, including any thinking block), so the tokens match. `from_pretrained` defaults to the model revision vLLM serves. The gate itself stays here (`train_probe` and `run_gate` in `probe.py`), because ModelLens's probes report accuracy, while the gate compares AUROC against token confidence.

ModelLens, torch and transformers are in the optional `probe` extra, pinned to one ModelLens commit. The gateway image and CI never install it. Install it on the GPU host, or on any machine with enough memory for Qwen3-1.7B:

```bash
uv sync --extra probe
```

```python
from app_assignment_1.probe import train_probe
from app_assignment_1.probe_modellens import ModelLensExtractor

# Uses the gateway's system prompt and vLLM's model revision by default.
extractor = ModelLensExtractor.from_pretrained(layer=14, pooling="last", device="cuda")
features = [extractor.extract(e.prompt, e.reply) for e in train_examples]
probe = train_probe(features, [e.correct for e in train_examples])
```

What was checked: the unit tests use a fake lens and a ChatML-style template, the format Qwen3 uses. By hand, on a tiny randomly initialized Qwen3 (4 layers, no download) with a ChatML template, the text had system, user and assistant turns, its last token was the reply's `}`, and the `last` vector matched the hidden state that transformers itself reports for that layer. The real Qwen3 tokenizer could not be downloaded in that check. It has not been run on Qwen3-1.7B, so there are no probe results.

Notes:

- The vector is the raw layer output. For the last layer, that is before the final norm, so it differs from the last entry of transformers' `hidden_states`.
- `last` pooling takes the final position, so pass one example at a time (as `extract` does), not a padded batch.
- `mean` averages over every token the model read, and the gateway's system prompt is most of them (about 7,500 characters, against a reply of about 100). The system prompt is the same for every request, so `mean` mostly measures it. A `mean` over the reply alone needs the reply's token offsets, like the intent-label position, and is not built.
- The prompt and the reply are tokenized separately and their IDs joined, the way vLLM saw them, so no token merges across the boundary. Re-encoding the reply text can still differ from the IDs the model actually generated. Before the gate run, compare `token_ids` with prompt and completion IDs captured from vLLM, at the same model revision and template settings, on a sample of requests.
- The template must render the same text vLLM rendered. `CHAT_TEMPLATE_KWARGS` holds the `chat_template_kwargs` the backend sends, and a test compares the two. The backend turns Qwen3's thinking off (`enable_thinking: False`), so the extractor does too.
- The proposed primary position, the last intent-label token, is not built yet. It needs the reply's token offsets.
- Record the model revision, layer and pooling in `GateRunInfo`.

## Still to build

- Run `ModelLensExtractor` on Qwen3-1.7B, offline, and pool at the last intent-label token.
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
- **Data split:** Eric owns the Banking77 development split (`docs/TEAM_PLAN.md`; proposed in #89). Use one fixed, seeded split, stratified by intent. The human grades draw from the same data, so agree it once with Sebastian and Yanzhen.

## Gate date

The gate run is due **within one week after both of these are ready**, as set in `docs/TEAM_PLAN.md`: Qwen3-1.7B runs on the GPU host, and the Banking77 development split exists. If the probe does not pass by then, it is reported as an offline result and stays out of the live system. The earlier date, Friday, November 20, 2026, moved because the GPU host is not deployed yet.
