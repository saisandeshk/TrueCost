"""The two-regime figure: the same cost-saving tricks on retail customer service (τ²-bench, the
cheap 9B matches the 27B) and on finance back-office work (AutomationBench finance, the 9B fails). Left: change in
verified success with 95% CIs; right: $ per verified task vs the baseline (log scale). Governor rows use the held-out
split each board rates it on. Drawn through the console's own board code, so it matches the board.
    python evaluation/scripts/regimes.py [~/.truecost/results.db]   -> results/figures/regimes.png
"""
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from truecost.console import Console  # noqa: E402

DB = str(Path(sys.argv[1] if len(sys.argv) > 1 else "~/.truecost/results.db").expanduser())
OUT = ROOT / "results" / "figures"
BLUE, ORANGE = "#2a78d6", "#eb6834"  # palette slots 1-2 (validated: dataviz skill)
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
ROWS = [("downgrade", "Downgrade to the 9B"), ("sy-stage", "NVIDIA Switchyard · stage router"),
        ("router", "von router"), ("sy-escalation", "NVIDIA Switchyard · escalation router"),
        ("mask", "Mask old tool outputs"), ("gov-a10", "TrueCost governor α 10%"), ("gov-a20", "TrueCost governor α 20%")]
BOARDS = [("tau2-retail", "Retail customer service (τ²-bench)", BLUE), ("ab-finance", "Finance back-office (AutomationBench)", ORANGE)]

c = Console([DB])
data, meta = {}, {}
for wl, _, _ in BOARDS:
    main, gov = c.crashtest(wl), c.crashtest(wl, subset="gov")
    rows = {r["condition"]: r for r in main["rows"]}
    rows.update({r["condition"]: r for r in gov["rows"] if r["condition"].startswith("gov")})
    data[wl] = rows
    meta[wl] = {"seeds": len(rows[main["baseline"]]["seeds"]), "noise_pp": (main.get("noise_floor") or {}).get("mean_abs_delta_pp")}

fig, (a1, a2) = plt.subplots(1, 2, figsize=(12.5, 5.6), dpi=150, sharey=True, gridspec_kw={"width_ratios": [1.15, 1]})
fig.suptitle("Same trick, opposite verdicts", x=0.01, ha="left", fontsize=14, fontweight="bold", color=INK)
fig.text(0.01, 0.915, "Each saving against its own baseline, on two kinds of enterprise work. Bars: 95% CIs. "
         f"Retail {meta['tau2-retail']['seeds']} seeds, finance {meta['ab-finance']['seeds']} seeds.",
         fontsize=9.5, color=INK2)
for ax in (a1, a2):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=9.5)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
off = {"tau2-retail": -0.17, "ab-finance": 0.17}
numbers = {}
for wl, label, col in BOARDS:
    for i, (cond, _) in enumerate(ROWS):
        r = data[wl].get(cond)
        if not r:
            continue
        cm = r["compare"]
        y = i + off[wl]
        d, ci = cm.get("success_delta_pp"), cm.get("success_delta_ci95") or [None, None]
        u, uci = cm.get("usd_per_verified_ratio"), cm.get("usd_per_verified_ratio_ci95") or [None, None]
        numbers.setdefault(wl, {})[cond] = {"delta_pp": d, "delta_ci": ci, "usd_ratio": u, "usd_ci": uci,
                                            "success": cm.get("success_run"), "seeds": r["seeds"]}
        a1.errorbar(d, y, xerr=[[d - ci[0]], [ci[1] - d]], fmt="o", color=col, ecolor=col, elinewidth=1.3,
                    markersize=7, markeredgecolor="white", markeredgewidth=1.5, zorder=3,
                    label=label if cond == "downgrade" else None)
        if u is not None and u == u and u != float("inf"):
            hi = uci[1] if uci[1] not in (None, float("inf")) else 40
            a2.errorbar(u, y, xerr=[[u - uci[0]], [hi - u]], fmt="o", color=col, ecolor=col, elinewidth=1.3,
                        markersize=7, markeredgecolor="white", markeredgewidth=1.5, zorder=3)
            a2.text(u * 1.12, y, f"×{u:.2f}", va="center", fontsize=7.8, color=INK2, zorder=4,
                    bbox=dict(boxstyle="square,pad=0.1", facecolor="white", edgecolor="none"))
        a1.text(ci[1] + 1.2, y, f"{d:+.1f}", va="center", fontsize=7.8, color=INK2)
a1.axvline(0, color=INK, linewidth=1)
a1.set_xlabel("Δ verified success vs baseline (percentage points)", color=INK2)
a1.set_yticks(range(len(ROWS)))
a1.set_yticklabels([n for _, n in ROWS], color=INK)
a1.invert_yaxis()
a1.set_xlim(-40, 22)
a2.set_xscale("log")
a2.axvline(1, color=INK, linewidth=1)
a2.set_xticks([0.25, 0.5, 1, 2, 4, 8])
a2.set_xticklabels(["×0.25", "×0.5", "×1", "×2", "×4", "×8"])
a2.set_xlim(0.2, 16)
a2.set_xlabel("$ per verified task vs baseline (log; left = cheaper)", color=INK2)
a1.legend(frameon=False, fontsize=9, loc="upper center", bbox_to_anchor=(0.95, -0.13), ncol=2)
fig.text(0.01, 0.01, "Governor rows: the held-out tasks each board rates it on. Finance downgrade's $ ratio CI is open "
         "ended (1 verified task in 100 per seed).", fontsize=7.8, color=INK2)
fig.tight_layout(rect=(0, 0.04, 1, 0.9))
fig.subplots_adjust(wspace=0.06)
fig.savefig(OUT / "regimes.png")
(ROOT / "results" / "regimes-numbers.json").write_text(json.dumps({"meta": meta, "rows": numbers}, indent=1, default=str))
print(json.dumps(meta))
