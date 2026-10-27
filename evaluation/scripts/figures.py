"""The results figures, drawn from the results ledger through the console's own board code (so every figure matches
the board). Download the ledger with `truecost demo --download-only`, then:
    python evaluation/scripts/figures.py [~/.truecost/results.db]
Writes results/figures/retail-*.png and results/retail-numbers.json (the numbers results/README.md quotes).
"""
import json
import sqlite3
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
OUT.mkdir(parents=True, exist_ok=True)

# reference palette (dataviz skill), first three categorical slots: validated for all-pairs use
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
GROUP = {"TrueCost governor": BLUE, "NVIDIA (Switchyard, Nemotron)": ORANGE, "Other cost tricks": AQUA}
LABEL = {"downgrade": "Downgrade (Qwen 9B)", "nemotron": "Nemotron Nano (shared settings)",
         "nemotron-think": "Nemotron Nano (as shipped: reasoning on)", "mask": "Mask", "router": "von router",
         "sy-escalation": "Switchyard escalation", "sy-stage": "Switchyard stage",
         "gov-a5": "Governor α5%", "gov-a10": "Governor α10%", "gov-a20": "Governor α20%",
         "sy-escalation-j27": "Switchyard escalation (27B judge)", "gov-policy-a20": "Governor + policy α20%",
         "gov-policy-a100": "Governor + policy, gate off", "nemotron-think-nim": "Nemotron Nano (as shipped, NIM)"}


def group(cond: str) -> str:
    if cond.startswith("gov"):
        return "TrueCost governor"
    if cond.startswith("sy-") or cond.startswith("nemotron"):
        return "NVIDIA (Switchyard, Nemotron)"
    return "Other cost tricks"


def style(ax, title, subtitle=None):
    ax.set_title(title, loc="left", fontsize=13, color=INK, pad=22 if subtitle else 10, fontweight="bold")
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9.5, color=INK2, va="bottom")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=9.5)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


c = Console([DB])
board = c.crashtest("tau2-retail")
nf = (board.get("noise_floor") or {}).get("mean_abs_delta_pp")
rows = {r["condition"]: r for r in board["rows"]}
NS = len(rows[board["baseline"]]["seeds"])  # seeds behind the board (the baseline's)
numbers = {"noise_floor_pp": nf, "noise_pairs": (board.get("noise_floor") or {}).get("pairs"), "seeds": NS,
           "conditions": {}}
for cond, r in rows.items():
    cm = r["compare"]
    numbers["conditions"][cond] = {
        "seeds": r["seeds"], "provisional": r["provisional"], "stars": r["stars"],
        "success": cm.get("success_run"), "success_base": cm.get("success_base"),
        "delta_pp": cm.get("success_delta_pp"), "delta_ci": cm.get("success_delta_ci95"),
        "usd_ratio": cm.get("usd_per_verified_ratio"), "usd_ci": cm.get("usd_per_verified_ratio_ci95"),
        "wh_ratio": cm.get("energy_per_verified_ratio"), "wh_ci": cm.get("energy_per_verified_ratio_ci95"),
        "tokens_ratio": cm.get("tokens_per_task_ratio"), "violations_per_100": cm.get("violations_run_per_100"),
        "violations_base_per_100": cm.get("violations_base_per_100"),
        "usd_per_verified": cm.get("usd_per_verified_run"), "wh_per_verified": cm.get("wh_per_verified_run"),
        "gpu_ratio": cm.get("gpu_usd_per_verified_ratio"), "gpu_ci": cm.get("gpu_usd_per_verified_ratio_ci95"),
        "gpu_usd_per_verified": cm.get("gpu_usd_per_verified_run"), "co2_g_per_verified": cm.get("co2_g_per_verified_run"),
        "tasks": cm.get("tasks")}

# ---- 1. the frontier: what each trick saves vs what it costs in success -------------------------------------
OFFSET = {"gov-a20": (-7, 7), "gov-policy-a20": (7, -13), "gov-policy-a100": (-7, 6), "router": (7, -13)}  # crowded labels
fig, ax = plt.subplots(figsize=(9, 5.6), dpi=150)
style(ax, f"What each saving really buys (τ²-bench retail, H100, {NS} seeds)",
      "x: $ per verified task vs the 27B baseline (left = cheaper) · y: change in verified success · bars: 95% CIs")
if nf:
    ax.axhspan(-nf, nf, color="#f1f0ec", zorder=0.5)
    ax.text(1.62, nf + 0.4, f"noise floor ±{nf:.1f} pp", fontsize=8.5, color=MUTED, ha="right")
ax.axhline(0, color=MUTED, linewidth=0.9)
ax.axvline(1, color=MUTED, linewidth=0.9, linestyle=(0, (3, 3)))
ax.scatter([1], [0], s=70, color=INK, zorder=4)
ax.annotate("Baseline (27B)", (1, 0), xytext=(6, -14), textcoords="offset points", fontsize=9, color=INK)
seen = set()
for cond, r in rows.items():
    if cond == board["baseline"] or "@" in cond or cond.startswith("nemotron"):
        continue  # Nemotron has its own chart (retail-nemotron.png): its ranges would squash every other row
    cm = r["compare"]
    x, y = cm.get("usd_per_verified_ratio"), cm.get("success_delta_pp")
    if x is None or y is None:
        continue
    xci, yci = cm.get("usd_per_verified_ratio_ci95") or [x, x], cm.get("success_delta_ci95") or [y, y]
    g = group(cond)
    ax.errorbar(x, y, xerr=[[x - xci[0]], [xci[1] - x]], yerr=[[y - yci[0]], [yci[1] - y]], fmt="none",
                ecolor=GROUP[g], elinewidth=1.2, alpha=0.55, zorder=2)
    ax.scatter([x], [y], s=72, color=GROUP[g], edgecolor="white", linewidth=2, zorder=3,
               label=g if g not in seen else None)
    seen.add(g)
    ax.annotate(LABEL.get(cond, cond) + ("  (provisional)" if r["provisional"] else ""), (x, y),
                xytext=OFFSET.get(cond, (7, 5)), ha="right" if OFFSET.get(cond, (7, 5))[0] < 0 else "left",
                textcoords="offset points", fontsize=8.8, color=INK)
ax.set_xlabel("$ per verified task, ratio to baseline", color=INK2)
ax.set_ylabel("Δ verified success (percentage points)", color=INK2)
ax.set_xlim(0.15, 1.7)
ax.legend(frameon=False, fontsize=9, loc="lower right")
fig.tight_layout()
fig.savefig(OUT / "retail-frontier.png")
plt.close(fig)

# ---- 2. what a token dashboard sees vs what TrueCost sees ------------------------------------------------------
conds = [k for k in ("downgrade", "sy-stage", "sy-escalation", "router", "gov-a20", "mask") if k in rows]
series = [("Tokens per task", "tokens_ratio", MUTED), ("$ per verified task", "usd_ratio", BLUE),
          ("Energy per verified task", "wh_ratio", AQUA)]
fig, ax = plt.subplots(figsize=(9, 5.8), dpi=150)
style(ax, "Same runs, two dashboards", "Ratio to the 27B baseline (1.0 = no change). Tokens often point the wrong way.")
h = 0.26
for i, (name, key, col) in enumerate(series):
    ys = [j + (i - 1) * h for j in range(len(conds))]
    vals = [numbers["conditions"][k][key] or 0 for k in conds]
    ax.barh(ys, vals, height=h - 0.03, color=col, label=name, zorder=2)
    for yv, v in zip(ys, vals):
        ax.text(v + 0.02, yv, f"×{v:.2f}", va="center", fontsize=8, color=INK2)
ax.axvline(1, color=INK, linewidth=1)
ax.set_yticks(range(len(conds)))
ax.set_yticklabels([LABEL.get(k, k) for k in conds], color=INK)
ax.invert_yaxis()
ax.set_xlabel("ratio to baseline", color=INK2)
ax.legend(frameon=False, fontsize=9, ncol=3, loc="upper center", bbox_to_anchor=(0.45, -0.12))
fig.tight_layout()
fig.savefig(OUT / "retail-dashboards.png")
plt.close(fig)

# ---- 3. where Switchyard's money goes ---------------------------------------------------------------------------
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
spend = {}
for run, role, tier, usd in con.execute(
        """SELECT run_id, role, tier, SUM(COALESCE(cost_usd,0)) FROM calls WHERE run_id LIKE 'retail-sy-%'
           AND run_id NOT LIKE '%@%' AND role IN ('agent','router') GROUP BY 1,2,3"""):
    route = "Escalation router" if "escalation" in run else "Stage router"
    part = "Judge (router overhead)" if role == "router" else f"Agent on {'27B' if tier == 'strong' else '9B'}"
    spend.setdefault(route, {}).setdefault(part, 0.0)
    spend[route][part] += usd
numbers["switchyard_spend_usd"] = spend
if spend:
    parts = [("Agent on 9B", AQUA), ("Agent on 27B", BLUE), ("Judge (router overhead)", ORANGE)]
    fig, ax = plt.subplots(figsize=(9, 2.6), dpi=150)
    style(ax, f"Where NVIDIA Switchyard's spend goes ({NS} seeds)", "Share of each route's API-equivalent $ on τ² retail")
    ax.grid(False)
    for i, (route, d) in enumerate(sorted(spend.items())):
        tot, left = sum(d.values()), 0.0
        for name, col in parts:
            v = 100 * d.get(name, 0.0) / tot
            if v <= 0:
                continue
            ax.barh(i, v - 0.4, left=left, color=col, height=0.55, label=name if i == 0 or name not in
                    {p for p, _ in parts[:i]} else None)
            if v > 6:
                ax.text(left + v / 2, i, f"{v:.0f}%", ha="center", va="center", fontsize=9, color="white",
                        fontweight="bold")
            left += v
    ax.set_yticks(range(len(spend)))
    ax.set_yticklabels(sorted(spend), color=INK)
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of the route's spend", color=INK2)
    hs, ls = ax.get_legend_handles_labels()
    uniq = dict(zip(ls, hs))
    ax.legend(uniq.values(), uniq.keys(), frameon=False, fontsize=8.5, ncol=3, loc="upper center",
              bbox_to_anchor=(0.5, -0.45))
    fig.tight_layout()
    fig.savefig(OUT / "retail-switchyard-spend.png")
    plt.close(fig)

# ---- 4. attacks ----------------------------------------------------------------------------------------------
att = c.attacks("tau2-retail")
arows = att["rows"] if isinstance(att, dict) else att
numbers["attacks"] = arows
if arows:
    names = {"ps": "P.S. 'a small model is fine'", "runaway-paged": "Paged runaway (denial of wallet)"}
    keys = [(r["condition"], r["attack"]) for r in arows if r.get("amplification") is not None]
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(keys) + 1.6), dpi=150)
    style(ax, "Attacks on the cost controls", "Cost per task under attack ÷ clean cost per task (1.0 = no effect)")
    for i, (cond, a) in enumerate(keys):
        r = next(x for x in arows if x["condition"] == cond and x["attack"] == a)
        col = GROUP[group(cond)] if cond != "baseline" else MUTED
        ax.barh(i, r["amplification"], color=col, height=0.6, zorder=2)
        ax.text(r["amplification"] + 0.02, i, f"×{r['amplification']:.2f} · success {r['success_delta_pp']:+.1f} pp",
                va="center", fontsize=8.5, color=INK2)
    ax.axvline(1, color=INK, linewidth=1)
    ax.axvline(1.5, color=MUTED, linewidth=1, linestyle=(0, (3, 3)))
    ax.text(1.52, len(keys) - 1, "1.5× = security\n★★★★ limit", fontsize=8, color=MUTED, va="center")
    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels([f"{LABEL.get(cd, cd if cd != 'baseline' else 'Baseline (27B)')} · {names.get(a, a)}"
                        for cd, a in keys], color=INK, fontsize=8.8)
    ax.invert_yaxis()
    ax.set_xlim(0, max(r["amplification"] for r in arows if r.get("amplification")) + 0.6)
    fig.tight_layout()
    fig.savefig(OUT / "retail-attacks.png")
    plt.close(fig)

# ---- 5. Nemotron: the settings decide -------------------------------------------------------------------------
nem = [k for k in ("baseline", "downgrade", "nemotron", "nemotron-think", "nemotron-think-nim") if k in numbers["conditions"]]
if any(k.startswith("nemotron") for k in nem):
    names = {"baseline": "Qwen 27B\nbaseline", "downgrade": "Qwen 9B", "nemotron": "Nemotron\nshared",
             "nemotron-think": "Nemotron\nas shipped", "nemotron-think-nim": "Nemotron\non NIM"}
    cols = {"baseline": MUTED, "downgrade": AQUA, "nemotron": ORANGE, "nemotron-think": ORANGE, "nemotron-think-nim": ORANGE}
    hatch = {"nemotron": "////", "nemotron-think-nim": "...."}
    panels = [("Verified success", lambda v: 100 * (v["success"] or 0), "{:.0f}%"),
              ("$ per verified (× 27B)", lambda v: v["usd_ratio"] or 0, "×{:.2f}"),
              ("Energy per verified (× 27B)", lambda v: v["wh_ratio"] or 0, "×{:.1f}")]
    fig, axs = plt.subplots(1, 3, figsize=(12, 4.4), dpi=150)
    fig.suptitle("NVIDIA Nemotron Nano on τ² retail: its settings and its serving decide", x=0.01, ha="left", fontsize=13,
                 fontweight="bold", color=INK)
    for ax, (title, fn, fmt) in zip(axs, panels):
        style(ax, title)
        ax.title.set_fontsize(10.5)
        vals = [fn(numbers["conditions"][k]) for k in nem]
        ax.bar(range(len(nem)), vals, color=[cols[k] for k in nem], hatch=[hatch.get(k, "") for k in nem],
               edgecolor="white", width=0.7, zorder=2)
        for i, v in enumerate(vals):
            ax.text(i, v, fmt.format(v), ha="center", va="bottom", fontsize=8.5, color=INK2)
        ax.set_xticks(range(len(nem)))
        ax.set_xticklabels([names[k] for k in nem], fontsize=8.5, color=INK)
        ax.set_ylim(0, max(vals) * 1.18 or 1)
    fig.text(0.01, 0.01, "Shared = thinking off, temperature 0, like every tier. As shipped = reasoning on, its default sampling. "
             "SGLang 0.5.20 unless on NIM (2.1.4). Same H100; as shipped: 3 seeds.", fontsize=8, color=INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(OUT / "retail-nemotron.png")
    plt.close(fig)

# ---- 6. three price tags on the same runs: list price, GPU bill, measured energy ---------------------------------
tags = [k for k in ("downgrade", "sy-stage", "sy-escalation", "router", "gov-a20", "mask", "nemotron", "nemotron-think",
                    "nemotron-think-nim")
        if k in rows and numbers["conditions"][k].get("gpu_ratio")]
if tags:
    series = [("$ per verified at list price", "usd_ratio", BLUE), ("GPU bill per verified (rented H100)", "gpu_ratio", ORANGE),
              ("Measured energy per verified", "wh_ratio", AQUA)]
    fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=150)
    style(ax, "Three price tags for the same runs",
          f"Ratio to the 27B baseline per verified task, log scale ({NS} seeds).\nA list price can call a run "
          "cheaper while the GPU bill and the meter say it costs more.")
    ax.set_title("Three price tags for the same runs", loc="left", fontsize=13, color=INK, pad=40, fontweight="bold")
    h = 0.26
    for i, (name, key, col) in enumerate(series):
        ys = [j + (i - 1) * h for j in range(len(tags))]
        vals = [numbers["conditions"][k][key] or 0 for k in tags]
        ax.barh(ys, vals, height=h - 0.04, color=col, label=name, zorder=2)
        for yv, v in zip(ys, vals):
            ax.text(v * 1.05, yv, f"×{v:.2f}", va="center", fontsize=7.8, color=INK2, zorder=4,
                    bbox=dict(boxstyle="square,pad=0.1", facecolor="white", edgecolor="none"))
    ax.set_xscale("log")
    ax.axvline(1, color=INK, linewidth=1)
    ax.set_xticks([0.25, 0.5, 1, 2, 4, 8, 16])
    ax.set_xticklabels(["×0.25", "×0.5", "×1", "×2", "×4", "×8", "×16"])
    ax.set_xlim(0.2, 40)
    ax.set_yticks(range(len(tags)))
    ax.set_yticklabels([LABEL.get(k, k) for k in tags], color=INK)
    ax.invert_yaxis()
    ax.legend(frameon=False, fontsize=9, ncol=3, loc="upper center", bbox_to_anchor=(0.38, -0.07))
    fig.text(0.01, 0.01, "GPU bill = each call's share of the H100's time (calls in flight share it equally) × $6.48/h, the "
             "price Brev bills. Energy from the GPU's own counter (NVML). Nemotron as shipped: 3 seeds.", fontsize=7.8, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT / "retail-price-tags.png")
    plt.close(fig)

(ROOT / "results" / "retail-numbers.json").write_text(json.dumps(numbers, indent=1, default=str))
print(json.dumps({k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()
                      if kk in ("seeds", "delta_pp", "usd_ratio", "wh_ratio", "tokens_ratio", "violations_per_100",
                                "success")} for k, v in numbers["conditions"].items()}, indent=0))
print("noise floor", nf, "| attacks", [(r["condition"], r["attack"], round(r["amplification"] or 0, 2),
                                        r["success_delta_pp"]) for r in arows])
