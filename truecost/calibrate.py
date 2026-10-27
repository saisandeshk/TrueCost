"""Fit the governor's gate and set its thresholds by conformal risk control.

Data: twin files written by the governor in record mode. Each line pairs a cheap draft with what the
strong model actually did at the same step ("match" = same action). Only steps whose draft is NOT a write
count, since write drafts are always pinned to the strong model and carry no gate risk.

1. Tasks (calibration units) are split deterministically into a fitting half and a calibration half.
2. A logistic gate P(match | draft features) is fitted on the fitting half.
3. Conformal risk control (Angelopoulos et al., 2022) on the calibration half, at task level: the loss of a
   task at threshold λ is the share of its gated steps that were accepted (score ≥ λ) but did not match the
   strong model. For each α, λ(α) is the smallest λ with  n/(n+1)·R̂(λ) + 1/(n+1) ≤ α,  which bounds the
   expected per-task share of wrong accepted steps by α for exchangeable new tasks.

The guarantee is about step deviations from the strong model (a conservative proxy for risk). Task-level
added failures are measured separately, on held-out tasks, by the crash-test.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

FEATURES = ["lp_mean", "lp_min", "lp_first", "lp_name_min", "lp_args_min", "low_frac", "n_tok_log", "is_tool",
            "step_log", "n_msgs_log", "last_tool_error", "von_large"]
DRAFT_ONLY = [f for f in FEATURES if f != "von_large"]
ALPHAS = (0.01, 0.02, 0.05, 0.10, 0.20)


def load_twins(paths: list[str], allowed_ids: set[str] | None = None) -> list[dict]:
    out = []
    for p in paths:
        run = Path(p).stem
        for line in open(Path(p).expanduser()):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            tid = r["task_key"].split("/", 1)[-1].split("#")[0]
            if allowed_ids is not None and tid not in allowed_ids:
                continue
            r["unit"] = f"{run.split('-')[0]}:{tid}"  # one unit per workload task, pooling its steps across seeds
            r["run"] = run
            out.append(r)
    return out


def _half(unit: str, salt: str = "truecost") -> int:
    return int(hashlib.sha1(f"{salt}:{unit}".encode()).hexdigest(), 16) % 2


def fit_logistic(X: list[list[float]], y: list[int], l2: float = 1.0, iters: int = 400, lr: float = 0.5):
    n, d = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(d)]
    std = [math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n) or 1.0 for j in range(d)]
    Z = [[(r[j] - mean[j]) / std[j] for j in range(d)] for r in X]
    w, b = [0.0] * d, 0.0
    for _ in range(iters):  # full-batch gradient descent on the L2-regularised log loss
        gw, gb = [l2 * wj / n for wj in w], 0.0
        for z, t in zip(Z, y):
            p = 1 / (1 + math.exp(-max(-50, min(50, b + sum(a * c for a, c in zip(w, z))))))
            e = p - t
            gb += e / n
            for j in range(d):
                gw[j] += e * z[j] / n
        w = [wj - lr * g for wj, g in zip(w, gw)]
        b -= lr * gb
    return w, b, mean, std


def _score(f: dict, w, b, mean, std, names: list[str] | None = None) -> float:
    z = b + sum(c * ((f.get(n, 0.0) - m) / s) for n, c, m, s in zip(names or FEATURES, w, mean, std))
    return 1 / (1 + math.exp(-max(-50, min(50, z))))


def auc(scores: list[float], labels: list[int]) -> float:
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def ece(scores: list[float], labels: list[int], bins: int = 10) -> float:
    """Expected calibration error of P(match): |mean score - match rate| weighted over equal-width bins."""
    if not scores:
        return float("nan")
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if lo <= s < hi or (b == bins - 1 and s == 1.0)]
        if idx:
            tot += len(idx) * abs(sum(scores[i] for i in idx) / len(idx) - sum(labels[i] for i in idx) / len(idx))
    return tot / len(scores)


def _platt(scores: list[float], labels: list[int]) -> tuple[float, float]:
    """Fit p' = sigmoid(a * logit(p) + b) by gradient descent on log loss."""
    z = [math.log(max(1e-6, min(1 - 1e-6, s)) / (1 - max(1e-6, min(1 - 1e-6, s)))) for s in scores]
    a, b = 1.0, 0.0
    for _ in range(500):
        ga = gb = 0.0
        for zi, y in zip(z, labels):
            p = 1 / (1 + math.exp(-max(-50, min(50, a * zi + b))))
            ga += (p - y) * zi
            gb += p - y
        a -= 0.5 * ga / len(z)
        b -= 0.5 * gb / len(z)
    return a, b


def crossfit_platt_ece(units: dict[str, list[tuple[float, int]]]) -> float:
    """ECE after a monotone (Platt) recalibration, cross-fitted over two halves of the calibration tasks.
    Monotone recalibration leaves every accept/escalate decision and the conformal bound unchanged."""
    keys = sorted(units)
    halves = [[k for i, k in enumerate(keys) if i % 2 == j] for j in (0, 1)]
    out_s, out_y = [], []
    for fit_keys, ev_keys in ((halves[0], halves[1]), (halves[1], halves[0])):
        fs = [s for k in fit_keys for s, _ in units[k]]
        fy = [y for k in fit_keys for _, y in units[k]]
        if not fs or not ev_keys:
            continue
        a, b = _platt(fs, fy)
        for k in ev_keys:
            for s_, y in units[k]:
                s_ = max(1e-6, min(1 - 1e-6, s_))
                out_s.append(1 / (1 + math.exp(-max(-50, min(50, a * math.log(s_ / (1 - s_)) + b)))))
                out_y.append(y)
    return ece(out_s, out_y)


def crc_threshold(units: dict[str, list[tuple[float, int]]], alpha: float) -> tuple[float, float]:
    """Smallest λ meeting the conformal risk bound; returns (λ, calibration risk at λ). λ = 1.01 means
    'never accept' (the bound cannot be met with this much calibration data)."""
    n = len(units)
    cands = sorted({s for steps in units.values() for s, _ in steps} | {1.01})
    best = (1.01, 0.0)
    for lam in reversed(cands):  # risk is non-increasing in λ: scan from strict to permissive
        risk = sum(sum(1 for s, m in steps if s >= lam and not m) / len(steps) for steps in units.values()) / n
        if n / (n + 1) * risk + 1 / (n + 1) <= alpha:
            best = (lam, risk)
        else:
            break
    return best


def calibrate(twins: list[dict], alphas=ALPHAS, features: list[str] | None = None) -> dict[str, Any]:
    feats = features if features is not None else [f for f in FEATURES if any(f in r["features"] for r in twins)]
    if not feats:
        raise ValueError("no gate features available")
    gated = [r for r in twins if not r.get("draft_write")]
    fit = [r for r in gated if _half(r["unit"]) == 0]
    cal = [r for r in gated if _half(r["unit"]) == 1]
    X = [[r["features"].get(n, 0.0) for n in feats] for r in fit]
    y = [1 if r["match"] else 0 for r in fit]
    w, b, mean, std = fit_logistic(X, y)
    cal_scores = [_score(r["features"], w, b, mean, std, feats) for r in cal]
    units: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for r, s in zip(cal, cal_scores):
        units[r["unit"]].append((s, 1 if r["match"] else 0))
    thresholds, report = {}, []
    for a in alphas:
        lam, risk = crc_threshold(units, a)
        acc = sum(1 for s in cal_scores if s >= lam) / len(cal_scores) if cal_scores else 0.0
        wrong = sum(1 for r, s in zip(cal, cal_scores) if s >= lam and not r["match"]) / len(cal_scores) if cal_scores else 0.0
        thresholds[str(a)] = lam
        report.append({"alpha": a, "threshold": lam, "calibration_risk": risk, "accept_rate_gated": acc,
                       "wrong_accepted_share": wrong, "feasible": lam <= 1.0})
    all_steps = len(twins)
    ablation = {}
    if "von_large" in feats and features is None:
        # Feature set chosen on the FITTING half only (inner 2-fold over fitting tasks), so the calibration
        # half stays untouched for the conformal thresholds.
        draft_feats = [f for f in feats if f != "von_large"]
        inner = {name: _inner_auc(fit, sub) for name, sub in
                 (("combined", feats), ("draft_only", draft_feats), ("decision_model_only", ["von_large"]))}
        chosen = "draft_only" if draft_feats and inner["draft_only"] >= inner["combined"] else "combined"
        ablation = {k: {"auc_fit_inner": v} for k, v in inner.items()}
        if chosen == "draft_only":
            best = calibrate(twins, alphas, draft_feats)
            best["ablation"] = {**ablation, "chosen": chosen}
            return best
        ablation["chosen"] = chosen
    return {
        "features": feats, "coef": w, "intercept": b, "mean": mean, "std": std, "thresholds": thresholds,
        "ablation": ablation,
        "report": report,
        "data": {"steps": all_steps, "gated_steps": len(gated), "pinned_share": 1 - len(gated) / all_steps if all_steps else 0,
                 "fit_steps": len(fit), "cal_steps": len(cal), "cal_units": len(units),
                 "match_rate": sum(r["match"] for r in gated) / len(gated) if gated else None,
                 "auc_cal": auc(cal_scores, [1 if r["match"] else 0 for r in cal]),
                 "ece_cal": ece(cal_scores, [1 if r["match"] else 0 for r in cal]),
                 "ece_cal_platt_crossfit": crossfit_platt_ece(units),
                 "mismatch_kinds": _kinds(gated), "writes": _writes(twins)},
    }


def _inner_auc(rows: list[dict], feats: list[str]) -> float:
    """2-fold AUC within the fitting half, split by task."""
    folds = [[r for r in rows if _half(r["unit"], "inner") == k] for k in (0, 1)]
    scores, labels = [], []
    for tr, te in ((folds[0], folds[1]), (folds[1], folds[0])):
        if not tr or not te:
            continue
        w, b, mean, std = fit_logistic([[r["features"].get(n, 0.0) for n in feats] for r in tr],
                                       [1 if r["match"] else 0 for r in tr])
        scores += [_score(r["features"], w, b, mean, std, feats) for r in te]
        labels += [1 if r["match"] else 0 for r in te]
    return auc(scores, labels)


def _writes(rows: list[dict]) -> dict[str, int]:
    """What pins protect: of the strong model's write steps, how often the cheap draft would have made the same
    write, a different write (wrong action or arguments), or no write."""
    w = [r for r in rows if r.get("strong_write")]
    same = sum(1 for r in w if r["match"])
    wrong = sum(1 for r in w if r.get("draft_write") and not r["match"])
    return {"strong_writes": len(w), "cheap_same_write": same, "cheap_wrong_write": wrong,
            "cheap_no_write": len(w) - same - wrong,
            "cheap_writes_where_strong_did_not": sum(1 for r in rows if r.get("draft_write") and not r.get("strong_write"))}


def _kinds(rows: list[dict]) -> dict[str, int]:
    k: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r["match"]:
            k[r["kind"]] += 1
    return dict(k)


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="truecost calibrate")
    ap.add_argument("twins", nargs="+")
    ap.add_argument("--split", help="τ² split file and name, e.g. data/tau2/domains/retail/split_tasks.json:train")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    allowed = None
    if a.split:
        path, name = a.split.rsplit(":", 1)
        allowed = {str(x) for x in json.loads(Path(path).expanduser().read_text())[name]}
    res = calibrate(load_twins(a.twins, allowed))
    Path(a.out).expanduser().write_text(json.dumps(res, indent=1))
    d = res["data"]
    print(f"steps {d['steps']} (gated {d['gated_steps']}, pinned {d['pinned_share']:.0%}); match rate {d['match_rate']:.2f}; "
          f"AUC {d['auc_cal']:.3f}, ECE {d['ece_cal']:.3f} (after monotone recalibration, cross-fitted: "
          f"{d['ece_cal_platt_crossfit']:.3f}) on {d['cal_units']} calibration tasks; mismatches {d['mismatch_kinds']}")
    for r in res["report"]:
        print(f"  alpha {r['alpha']:.2f}: threshold {r['threshold']:.3f}  accept {r['accept_rate_gated']:.0%} of gated steps  "
              f"calibration risk {r['calibration_risk']:.3f}{'' if r['feasible'] else '  (infeasible: never accept)'}")


if __name__ == "__main__":
    main()
