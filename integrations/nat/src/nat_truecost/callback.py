"""Authoritative run-level reporting using TrueCost's existing report functions."""
import json
import math
from pathlib import Path

from nat.plugin_api import TelemetryExporterBaseConfig
from truecost.report import compare, run_metrics

from ._ledger import read_ledger


class SummaryConfig(TelemetryExporterBaseConfig, name="truecost_summary"):
    ledger: str
    run_id: str
    baseline_run_id: str | None = None


def json_safe(value):
    """Keep strict JSON while preserving undefined and infinite comparison results explicitly."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    return value


class SummaryCallback:
    def __init__(self, config: SummaryConfig):
        self.config = config

    def on_eval_complete(self, result):
        if result.output_dir is None:
            raise ValueError("truecost_summary requires NAT's eval output directory")
        with read_ledger(self.config.ledger) as db:
            metrics = run_metrics(db, self.config.run_id)
            comparison = (compare(db, self.config.baseline_run_id, self.config.run_id)
                          if self.config.baseline_run_id else None)
        summary = dict(metrics)
        summary["scope"] = "All graded tasks in the configured ledger run; independent of NAT dataset filtering."
        if comparison is not None:
            summary["comparison"] = comparison
        out = Path(result.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "truecost_summary.json").write_text(json.dumps(json_safe(summary), indent=2, allow_nan=False) + "\n")
        print(f"\nTrueCost — {self.config.run_id} (full ledger run)")
        print("| metric | value |\n|---|---|")
        fields = ("usd_per_verified", "energy_wh_per_verified", "success_rate",
                  "violations_per_100", "wasted_cost_share")
        for key in fields:
            value = metrics.get(key)
            if value is None:
                value = "unmeasured" if key.startswith("energy") else "unknown"
            print(f"| {key} | {value} |")
        if comparison:
            print(f"Baseline: {self.config.baseline_run_id}; paired tasks: {comparison['paired_tasks']}")
            for key in ("usd_per_verified_ratio", "energy_per_verified_ratio", "success_delta_pp"):
                if key in comparison:
                    print(f"| {key} | {comparison[key]} |")
                    if key + "_ci95" in comparison:
                        print(f"| {key}_ci95 | {comparison[key + '_ci95']} |")
        print(f"Summary: {out / 'truecost_summary.json'}")
