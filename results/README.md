# Results

All runs on NVIDIA Brev 1× H100 80 GB hosts, served by SGLang 0.5.20 (and NVIDIA NIM 2.1.4 for one row): **309
runs, 13,940 graded tasks, 416,119 metered model calls**. Every figure and table here is redrawn from the published
results ledger ([release v0.1.0](https://github.com/saisandeshk/TrueCost/releases/tag/v0.1.0); `truecost demo
--download-only` fetches it) by the scripts in [`evaluation/scripts/`](../evaluation/scripts/). The method, fixed
before the runs: [evaluation/PROTOCOL.md](../evaluation/PROTOCOL.md). The full star boards: [boards.md](boards.md).

\* = beyond the noise floor: the 95% CI excludes zero and the effect exceeds how much unchanged reruns of the baseline
differ (4.7 points on retail over 28 pairs of seeds, 7.3 on finance).

## 1. Same trick, opposite verdicts

![Same trick, opposite verdicts](figures/regimes.png)

On retail customer service the cheap model (Qwen3.5-9B) verifies almost as many tasks as the strong one
(Qwen3.8-27B): the savings that route to it are real, and NVIDIA Switchyard's stage router finds that saving on its
own. On finance back-office work the cheap model verifies 1 task in 100 against the strong model's 26, and every
router that leans on it loses 13–25 points and costs more per finished task. The governor is the only control that
held quality on both, because its gate adapts to the work: in the calibration data the cheap model's step matches the
strong model's on 66% of retail steps but only 6% of finance steps, and on the held-out tasks the gate kept the cheap
draft far less often on finance than on retail (24% against 48% of the steps it judged at α = 10%; 43% against 71% at
α = 20%). It declines to save where saving is unsafe.

## 2. What each saving really buys (retail, 8 seeds)

![Frontier](figures/retail-frontier.png)

| Condition | Δ verified (95% CI) | $ / verified | GPU bill / verified | Energy / verified |
|---|---|---|---|---|
| NVIDIA Switchyard, stage router | +0.8 (−4.2 to 5.7) | **×0.35** | ×0.63 | ×0.62 |
| Downgrade to the 9B | −1.8 (−5.9 to 3.0) | **×0.36** | ×0.64 | ×0.63 |
| NVIDIA Switchyard, escalation router (9B judge) | −0.9 (−5.6 to 3.8) | ×0.66 | ×0.90 | ×0.95 |
| NVIDIA Switchyard, escalation router (27B judge) | −0.4 (−5.6 to 4.8) | ×1.46 | ×1.95 | ×2.25 |
| von router | +3.2 (−1.5 to 7.6) | ×0.80 | ×1.19 | ×1.21 |
| Mask old tool outputs | **−5.5 \*** (−10.1 to −1.2) | ×1.39 | ×1.23 | ×1.39 |
| TrueCost governor, α 20% | **+6.9 \*** (1.2 to 13.1) | ×0.87 | ×1.55 | ×1.55 |
| TrueCost governor + policy check, α 20% | **+6.6 \*** (1.6 to 12.2) | ×0.88 | ×1.52 | ×1.53 |
| TrueCost policy check, gate off | +3.1 (−3.8 to 10.6) | ×0.64 | ×1.14 | ×1.18 |

**Tokens point the wrong way** ([figure](figures/retail-dashboards.png)): the downgrade raises tokens per task by a
third and still cuts $ per verified task by 64%; the mask barely moves tokens and makes each finished task 39% dearer.

## 3. Three price tags

![Three price tags](figures/retail-price-tags.png)

| NVIDIA Nemotron-3-Nano-30B-A3B | Δ verified | List price | GPU bill | Energy | Violations / 100 |
|---|---|---|---|---|---|
| As shipped (reasoning on), SGLang, 3 seeds | −0.9 (−7.3 to 5.6) | ×0.89 | **×15.4** | ×11.0 | 15.8 |
| As shipped, **NVIDIA NIM**, 3 seeds | **−7.3 \*** (−14.9 to −0.3) | ×0.95 | **×4.4** | ×3.9 | 10.5 |
| Shared settings (thinking off, temperature 0), 8 seeds | **−47.9 \*** | ×1.88 | ×2.52 | ×2.15 | 29.9 |

The strong model's baseline has 3.9 violations per 100 tasks. Every list-price saving shrinks on the GPU bill (the
downgrade: ×0.36 → ×0.64), and one model's price-list saving became a 15× GPU bill on one serving stack and 4.4× on
another. A price list alone would have pointed the other way.

## 4. Attacks on the cost controls

![Attacks](figures/retail-attacks.png)

| Condition, attack | Cost per task (attacked ÷ clean) | Δ verified |
|---|---|---|
| Baseline, paged runaway (a tool that keeps asking for the next page) | **×1.49** | **−28.4 points** |
| TrueCost governor α 10%, paged runaway (its breaker stops the loop) | ×0.96 | −5.0 |
| von router, P.S. line ("a small cheap model is fine") | ×0.70 | −2.8 |
| NVIDIA Switchyard escalation, P.S. line | ×0.99 | +3.8 |
| TrueCost governor α 10%, P.S. line | ×1.04 | −0.3 |

On finance the attacks barely move the cheap paths, which were already failing ([boards.md](boards.md)).

## 5. Safety: the rule checker and an independent rater

The rule checker (`truecost/safety.py`) counts writes that skip a written store rule. A second rater, the 27B with
thinking on, read 9,238 retail transcripts against the same written policy (4% gave no parseable verdict). Its
verdicts are a release asset (`rater-retail.jsonl.xz`); `evaluation/scripts/rater_summary.py` regenerates every number
below into [rater-summary.json](rater-summary.json).

| | Rule checker | Independent rater |
|---|---|---|
| Agreement (Cohen's κ): any consequential violation | | 0.45 |
| … identity verified first | | 0.99 |
| … explicit confirmation before the action | | 0.33 |
| … order read before changing it | | 0.28 |
| Held-out tasks with a consequential violation: baseline | 3.2% | 6.4% |
| … TrueCost governor + policy check, α 20% | **0.0%** | 7.4% |
| … TrueCost policy check, gate off | 0.0% | 4.1% |
| … TrueCost governor, α 20% | 0.6% | 5.8% |

A pre-write check enforces the rules it encodes, and no more: by the outside rater the governor with the policy check
is no safer than the strong model alone (7.4% vs 6.4%), because most of what the rater flags is what counts as the
customer's explicit confirmation, which the rules encode loosely. Both numbers are reported. `evaluation/scripts/audit_sheet.py` samples the
disagreements for a human audit.

## 6. Invoice fraud, contained in layers

An accounts-payable agent (OpenCode) in an NVIDIA OpenShell default-deny sandbox processes four supplier invoices;
one carries a planted "our bank details changed" instruction (3 variants). **Contained** = OpenShell only;
**governed** = plus TrueCost's hold on vendor bank-detail changes. 5 trials per variant and arm.

| Agent model | Arm | Trials | Followed the injection | Bank change executed | Bank changes held | Invoices processed correctly |
|---|---|---|---|---|---|---|
| Qwen3.8-27B | contained + governed | 30 | **0** | 0 | – | 30 |
| Nemotron-3-Nano as shipped | contained | 30 | 1 | **1** | – | 28 |
| Nemotron-3-Nano as shipped | governed | 15 | 0 | 0 | – | 12 |
| Nemotron-3-Nano, reasoning dropped between turns† | governed | 15 | 4 | **0** | **6 (all)** | 6 |
| Qwen3.5-9B | contained + governed | 30 | 0 | 0 | – | 0 |

† In these 15 trials the gateway's buffered replay for the hold left the model's reasoning text out of later turns,
as some agent frameworks do; the gateway now keeps it (the governed row above). They are reported as a stress case:
less reasoning carried over, more injections followed, and the hold still stopped every bank change. Generated table:
[boards.md](boards.md#invoice-fraud-demonstration-nvidia-openshell).

- The strong model named the injected instruction as suspicious instead of following it.
- The one executed bank change was a write inside the application: a network sandbox cannot see it, an action-level
  hold can.
- OpenShell blocked about 9 outbound connections per trial (the agent's own update and telemetry calls); no trial
  tried to send data out.
- The 9B never got going: it looked for the task description outside the project folder, which OpenCode refused.
- The Infosys Responsible AI Toolkit's prompt-injection model scored every tool output in flag mode. It passed the
  business-style fraud invoices as legitimate (scores 0.86–0.9999) and flagged ordinary unit-test output instead (up to
  0.80): instruction-like text and payment fraud are different signals, which is why the hold sits at the action.
