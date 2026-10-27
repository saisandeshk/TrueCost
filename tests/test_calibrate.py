import math
import random

from truecost.calibrate import _score, calibrate


def synth(n_tasks, seed, steps=12):
    rng = random.Random(seed)
    rows = []
    for t in range(n_tasks):
        for k in range(steps):
            lp = -abs(rng.gauss(0.25, 0.3))
            p_match = 1 / (1 + math.exp(-(6 * (lp + 0.35))))
            rows.append({"unit": f"run|{seed}-{t}", "task_key": f"run/{t}", "match": rng.random() < p_match,
                         "kind": "tool_args", "draft_write": rng.random() < 0.1,
                         "features": {"lp_mean": lp, "lp_min": lp * 2, "is_tool": 1.0}})
    return rows


def test_thresholds_monotone_and_risk_holds_on_fresh_tasks():
    res = calibrate(synth(300, 1))
    th = [res["thresholds"][str(a)] for a in (0.01, 0.02, 0.05, 0.10, 0.20)]
    assert th == sorted(th, reverse=True)          # larger alpha -> more permissive
    assert res["data"]["auc_cal"] > 0.6
    fresh = [r for r in synth(400, 2) if not r["draft_write"]]
    for a in (0.05, 0.10, 0.20):
        lam = res["thresholds"][str(a)]
        by = {}
        for r in fresh:
            s = _score(r["features"], res["coef"], res["intercept"], res["mean"], res["std"], res["features"])
            by.setdefault(r["unit"], []).append(s >= lam and not r["match"])
        risk = sum(sum(v) / len(v) for v in by.values()) / len(by)
        assert risk <= a + 0.02, (a, risk)        # expected per-task wrong-accept share stays near alpha


def test_sign_flip_and_task_units(tmp_path):
    import json
    from truecost.calibrate import load_twins
    from truecost.stats import sign_flip_p
    assert sign_flip_p([0, 0]) == 1.0
    assert sign_flip_p([1.0] * 10) < 0.01 and sign_flip_p([1, -1, 1, -1]) == 1.0
    for run in ("retail-baseline-s300", "retail-baseline-s301"):
        (tmp_path / f"{run}.jsonl").write_text(json.dumps({"task_key": f"{run}/7", "match": True, "kind": "text",
                                                           "features": {}}) + "\n")
    rows = load_twins([str(p) for p in tmp_path.glob("*.jsonl")])
    assert {r["unit"] for r in rows} == {"retail:7"}  # one unit per workload task, across seeds
