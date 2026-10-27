# Evaluation: reproduce every number

Everything behind [`results/`](../results/README.md): the protocol, the gateway configs, the crash-test grids, the
frozen governor gates, the host scripts and the analysis scripts.

| Path | What |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | What is compared, the star thresholds, seeds, splits and statistics, fixed before the runs |
| `configs/` | Gateway configs for one H100: `h100.toml` (retail), `h100-think.toml` (finance: the strong tier thinks), `switchyard.toml` (NeMo Switchyard's routes) |
| `grids/` | Crash-test grids: one cell per condition, with its savings, governor settings and seeds; `splits/` holds the finance train/test split |
| `gates/` | The governor gates, fitted once from training tasks and frozen before any governor run |
| `serving/` | Host setup, the model-tier layouts (SGLang), NVIDIA NIM for Nemotron, the von router service |
| `run_boards.sh` | Runs every cell of a board for given seeds, in the protocol's order; resumable |
| `scripts/` | Figures, the second safety rater, the human audit sheet, the deterministic split, ledger merging |

## Reproduce on an H100

One NVIDIA Brev 1× H100 80 GB instance per seed batch. Clone this repository to `~/TrueCost` (the grids use that
path); everything else goes under `~/truecost-work`.

```bash
bash evaluation/serving/setup.sh --with-von --with-nvidia   # TrueCost, SGLang, models, τ²-bench, AutomationBench,
                                                           # von, Nemotron, Switchyard, OpenShell, nvcr.io login
bash evaluation/serving/tiers.sh up h100                   # strong 27B + cheap 9B on the one GPU

SEEDS="303 304" bash evaluation/run_boards.sh retail       # every retail cell for these seeds
SEEDS="303 304" NEMOTRON=1 bash evaluation/run_boards.sh retail   # then Nemotron on SGLang (swaps the 27B out)
SEEDS="303 304 305" bash evaluation/serving/nim_nemotron.sh       # Nemotron on NVIDIA NIM
SEEDS="321 322 323" bash evaluation/run_boards.sh finance  # the finance board (strong tier: 27B thinking on)
```

Spread seeds over several hosts, then merge their ledgers and draw the boards:

```bash
python evaluation/scripts/merge_ledgers.py ~/.truecost/results.db host1/ledger.db host2/ledger.db
truecost board tau2-retail --ledger ~/.truecost/results.db       # the star board as markdown
python evaluation/scripts/figures.py ~/.truecost/results.db      # results/figures/retail-*.png + retail-numbers.json
python evaluation/scripts/regimes.py ~/.truecost/results.db      # results/figures/regimes.png
```

To check our numbers without a GPU, fetch the published ledger (`truecost demo --download-only`) and run the last
three commands on it: they redraw every figure and table in `results/` from the raw call records.

## Invoice-fraud demonstration

```bash
MODELS="strong cheap nemotron-think" ARMS="contained governed" TRIALS=5 \
  SHIELD_URL=http://127.0.0.1:8000 bash integrations/openshell/run_ap_demo.sh
```

`SHIELD_URL` points at a running [Infosys Responsible AI Toolkit](https://github.com/Infosys/Infosys-Responsible-AI-Toolkit)
moderation service; leave it unset to run without the shield.
