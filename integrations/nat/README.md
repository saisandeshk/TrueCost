# TrueCost for NeMo Agent Toolkit

See how much money and measured GPU energy a finished agent run spent for each
independently verified result, and how often it broke policy. Failed attempts
count toward the bill. This plugin reads TrueCost's ledger and prints the same
run metrics as `truecost report`.

TrueCost ships as a NAT plugin that adds the $ and energy dimensions. This is an
offline replay of finished τ² crash tests; it does not run τ² on NAT. Replay never
contacts an LLM, gateway, benchmark service, or remote machine.

## Install and run

From the repository root, with Python 3.12 (3.11–3.13 supported). The demo reads the published results ledger:
`truecost demo --download-only` fetches it to `~/.truecost/results.db`.

```bash
uv venv --python 3.12 --seed integrations/nat/.venv
uv pip install -p integrations/nat/.venv/bin/python -e . -e 'integrations/nat[dev]'
NAT_TELEMETRY_ENABLED=false integrations/nat/.venv/bin/nat eval --config_file integrations/nat/configs/tau2_retail_eval.yml
```

Without `uv`, use `python3.12 -m venv integrations/nat/.venv` and its `pip`. NAT is pinned to
`nvidia-nat[eval,profiler]==1.9.0`, and the local TrueCost package is installed alongside the plugin. The explicit
`langchain-core` dependency covers NAT 1.9.0's unconditional eval-adapter import, which its eval/profiler extras do
not pull in. The command disables NAT's CLI telemetry so the replay stays offline.

The demo replays run `retail-downgrade-s303`, paired against `retail-baseline-s303`. Edit the YAML's ledger/run
anchors and baseline to evaluate another finished run. The dataset, replay and summary all open SQLite with
`mode=ro` and `query_only=ON`: no schema creation, settings changes or writes. The summary is written to
`integrations/nat/output/tau2_retail/truecost_summary.json`.

## What the numbers mean

| Summary field | Meaning |
|---|---|
| `usd_per_verified` | Agent-side API-equivalent dollars for **all** graded tasks, divided by verified results. |
| `energy_wh_per_verified` | Agent-side measured watt-hours for all graded tasks, divided by verified results. Null means **unmeasured**, including any unmetered call or zero total recorded energy. |
| `success_rate` | Share of graded tasks whose independent check passed (0 to 1). |
| `violations_per_100` | Recorded consequential policy violations per 100 graded tasks. |
| `wasted_cost_share` | Share of recorded dollars spent on failed tasks (0 to 1). |

Agent-side roles are TrueCost's `agent`, `summariser`, `governor` and `router` (a routing judge's own model
calls), the same set `truecost report` counts.
Customer simulation and grader calls are excluded. Unknown model prices produce
null dollar totals; they are not free calls. The report's existing conventions
are preserved, including dividing by `max(verified, 1)` when nothing is verified.
The failed-spend share also preserves the core report's known-cost convention
when some calls are unpriced.

The JSON includes every field returned by `run_metrics()`, and optional
`comparison` from `compare()` with paired ratios and bootstrap confidence
intervals. Nonfinite statistics are strings (`nan`, `inf`, `-inf`) so the artifact
is strict JSON. The console prints full precision for the five main metrics and
paired cost/energy ratios and intervals. The underlying values match
`truecost report`; its console presentation rounds some values.

The authoritative summary always covers the **whole configured ledger run**.
NAT item filters and repeated replay items do not change it. Per-item evaluator
files contain verified indicators, task dollars, task Wh, and violations.
Cost and energy `average_score` are deliberately null: a mean of task costs is
not cost per verified outcome. Their reasoning points to the run-level summary.
Verified and violation aggregates retain full precision. Unknown item energy is
null with `unmeasured` reasoning, never zero.

`truecost_ledger_run` emits one row per verified/failed task; `id` is `task_key`,
`question` is title or task id, `answer` is `verified`, and NAT retains the entire
row plus `per_task()` aggregates in `full_dataset_entry`. The demo feeds `id` as
the workflow input so duplicate titles cannot confuse `truecost_replay`.

## Record another NAT workflow

Add this tracing exporter to a workflow that does **not** use the TrueCost gateway:

```yaml
general:
  telemetry:
    tracing:
      ledger:
        _type: truecost_ledger
        ledger: /path/to/writable/ledger.db
        run_id: my-nat-run
```

Each `LLM_END` event books one `source=nat`, `role=agent` call via `Ledger.add_call`.
Prompt tokens include cached tokens in NAT; the exporter subtracts cached tokens
before calling TrueCost pricing. Model name comes from event metadata or response
payload, falling back to the event name. Missing usage/prices remain unknown.
Latency uses the paired start timestamp, or NAT's end-event span timestamp.
Energy stays null: NAT does not measure GPU joules; TrueCost's gateway does.

Task attribution prefers `task_key` / `item_id` in event metadata (also under
`provided_metadata`), then NAT's `workflow_run_id` trace UUID. NAT 1.9.0 does not
attach a universal dataset item id to LLM steps. To join calls to externally
graded outcomes, import task rows with the same keys. The exporter records calls;
it does not invent verified task outcomes. Stable run/task/event IDs make repeated
export of the same event idempotent.

**Avoid double booking:** this exporter is for workflows that bypass the gateway.
It skips events carrying `X-TrueCost-Task` (case insensitive, including nested
metadata/payload and request headers) or `truecost_booked: true`. Frameworks may
omit outgoing headers from telemetry; absence of that header does not prove a
call bypassed the gateway. Do not enable this exporter on gateway-routed workflows.
Only `truecost_ledger` opens a writable ledger, at the explicitly configured path.
`truecost_summary` is a separate, callback-only tracing component.

## Verification

`bash integrations/nat/verification/verify.sh` reruns every acceptance check and writes its evidence next to it:

| Check | Evidence |
|---|---|
| Plugin tests (30) | `plugin-tests.txt` |
| Every NAT symbol and method the plugin uses, checked against the installed 1.9.0 source | `api-audit.txt`, `NAT_API.md` |
| Offline `nat eval` over all 114 graded tasks of the demo run | `nat-eval.txt` |
| Exact parity with `truecost report` on the same ledger: every run field and every paired-comparison field, bootstrap CIs included | `parity.txt`, `core-report.json` |
| The ledger is untouched (size and SHA-256 before and after) | `ledger-before.json`, `ledger-after.json` |
