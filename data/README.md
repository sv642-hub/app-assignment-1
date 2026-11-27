# Data

## banking77_split.csv

Our development split of Banking77, built by `scripts/make_banking77_split.py`.

- **Source:** Banking77 by PolyAI, `banking_data/train.csv` and `test.csv` at commit
  `57ec275` of https://github.com/PolyAI-LDN/task-specific-datasets (the script checks
  their SHA-256).
- **Paper:** Iñigo Casanueva, Tadas Temčinas, Daniela Gerz, Matthew Henderson and Ivan
  Vulić. 2020. "Efficient Intent Detection with Dual Sentence Encoders." In
  *Proceedings of the 2nd Workshop on NLP for ConvAI*. https://arxiv.org/abs/2003.04807
- **Licence:** CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). The texts are
  unchanged; we add our intent label, a `train` or `dev` split and the source row.

Columns: `text`, `intent` (our label from `config/labels.json`), `split` (`train` or
`dev`) and `source_row` (the 0-based data row in Banking77's `train.csv`).

How it is made: messages that match an official test message, or repeat an earlier
train message, are dropped first. Two messages match when they are equal after NFKC,
casefolding, turning dashes into spaces, dropping other punctuation and collapsing
whitespace. Then 20% of each intent goes to `dev`, shuffled with seed 6700, and the
rest to `train`. The official test set is not in this file and stays the benchmark.

Once results depend on this file it must not change. `tests/test_banking77.py` pins
its SHA-256.
