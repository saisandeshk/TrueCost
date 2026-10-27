"""Exercise installed entry-point discovery, real NAT dataset handling, and callbacks."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from truecost.report import compare, run_metrics

ROOT = Path(__file__).resolve().parents[3]


def test_real_nat_eval_offline(ledger, tmp_path):
    cfg = yaml.safe_load((ROOT / "integrations/nat/configs/tau2_retail_eval.yml").read_text())
    output = tmp_path / "output"
    cfg["workflow"].update(ledger=str(ledger.path), run_id="run")
    cfg["eval"]["general"]["dataset"].update(ledger=str(ledger.path), run_id="run")
    cfg["eval"]["general"]["output"]["dir"] = str(output)
    cfg["general"]["telemetry"]["tracing"]["truecost_summary"].update(
        ledger=str(ledger.path), run_id="run", baseline_run_id="base")
    config = tmp_path / "eval.yml"
    config.write_text(yaml.safe_dump(cfg))
    env = dict(os.environ, NAT_TELEMETRY_ENABLED="false", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(ROOT / "integrations/nat/verification/offline_guard"))
    proc = subprocess.run([str(Path(sys.executable).parent / "nat"), "eval", "--config_file", str(config)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Workflow Status: COMPLETED" in proc.stdout
    assert "Failed to" not in proc.stderr
    assert "Offline verification forbids" not in proc.stderr
    result = json.loads((output / "truecost_summary.json").read_text())
    assert {k: result[k] for k in run_metrics(ledger, "run")} == run_metrics(ledger, "run")
    assert result["comparison"] == json.loads(json.dumps(compare(ledger, "base", "run")))
    wf = json.loads((output / "workflow_output.json").read_text())
    # NAT serializes dataset rows with recorded generated answers.
    assert {x["id"]: x["generated_answer"] for x in wf} == {
        "run/0": "verified", "run/1": "verified", "run/2": "failed"}
    for name in ("verified", "cost_per_verified", "energy_per_verified", "violations"):
        scores = json.loads((output / f"truecost_{name}_output.json").read_text())
        assert len(scores["eval_output_items"]) == 3
        assert all(x.get("error") is None for x in scores["eval_output_items"])


def test_pinned_nat_and_entrypoint():
    from importlib.metadata import entry_points, version
    for package in ("nvidia-nat", "nvidia-nat-core", "nvidia-nat-eval", "nvidia-nat-profiler"):
        assert version(package) == "1.9.0"
    plugin = next(ep for ep in entry_points(group="nat.plugins") if ep.name == "nat_truecost")
    assert plugin.load().__name__ == "nat_truecost.register"
