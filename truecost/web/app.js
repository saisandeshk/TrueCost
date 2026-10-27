import { h, stackedColumns, dotPlot, scatter, groupedBars } from "./charts.js";

const main = document.querySelector("main");
const store = {
  get(k, d) { try { return localStorage.getItem(`tc.${k}`) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`tc.${k}`, v); } catch { /* private mode */ } },
};
const state = { range: store.get("range", "7d"), source: store.get("source", "all"), workload: store.get("workload", null) };
let timer = null;

const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(`${r.status} ${p}`); return r.json(); };
const SOURCES = { all: "All usage", claude_code: "Claude Code", codex: "Codex", gateway: "Agent fleet (gateway)",
  crashtest: "Crash-test runs" };
const RANGES = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All"]];

// ---- formatting ---------------------------------------------------------------------------------
const usd = (v) => v == null ? "–" : v >= 100 ? `$${v.toFixed(0)}` : v >= 1 ? `$${v.toFixed(2)}` : v >= 0.01 ? `$${v.toFixed(3)}` : v > 0 ? `$${v.toFixed(4)}` : "$0";
const pct = (v, d = 0) => v == null ? "–" : `${(100 * v).toFixed(d)}%`;
const num = (v) => v == null ? "–" : v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : `${Math.round(v)}`;
const wh = (v) => v == null ? "–" : v >= 1 ? `${v.toFixed(2)} Wh` : `${(v * 1000).toFixed(0)} mWh`;
const ratio = (v) => v == null ? "–" : `×${v.toFixed(2)}`;
const pp = (v) => v == null ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(1)} pp`;
const clock = (t) => t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
const when = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
const outcome = (o) => h("span", { class: `st ${o || "unverified"}` }, h("i"), { verified: "verified", failed: "failed", unverified: "unverified", running: "running" }[o] || o || "–");
// A governor/saver decision in words: "accept 0.79 ≥ 0.67", "escalate 0.57 < 0.67", "pin: refund", saver names.
function decisionChip(c, detail = false) {
  const d = c.decision;
  if (d && d.pin) return h("span", { class: "chip pin" }, `pin: ${d.pin}`);
  if (d) {
    const sc = d.score != null ? d.score.toFixed(2) : "";
    const cmp = detail && d.score != null && d.threshold != null && d.threshold <= 1
      ? ` ${d.score >= d.threshold ? "≥" : "<"} ${d.threshold.toFixed(2)}${d.alpha != null ? ` (α ${Math.round(100 * d.alpha)}%)` : ""}` : "";
    return h("span", { class: "chip" }, `${d.action || ""} ${sc}${cmp}`);
  }
  return c.saver ? h("span", { class: "chip" }, Object.keys(c.saver).join(", ")) : "";
}
const stars = (n) => n == null ? h("span", { class: "muted" }, "–") : h("span", { class: "stars", title: `${n} of 5` }, "★".repeat(n), h("span", { class: "off" }, "★".repeat(5 - n)));

function card(title, sub, ...body) {
  return h("section", { class: "card" }, title ? h("h2", {}, title) : null, sub ? h("p", { class: "sub" }, sub) : null, ...body);
}
function tile(label, value, detail) {
  return h("div", { class: "tile" }, h("div", { class: "l" }, label), h("div", { class: "v" }, value), detail ? h("div", { class: "d" }, detail) : null);
}
function seg(options, current, onPick) {
  return h("div", { class: "seg" }, options.map(([v, label]) =>
    h("button", { class: v === current ? "on" : "", onclick: () => onPick(v) }, label)));
}
function filters({ source = true } = {}) {
  return h("div", { class: "filters" },
    h("label", {}, "Range"), seg(RANGES, state.range, (v) => { state.range = v; store.set("range", v); route(); }),
    source ? h("label", {}, "Source") : null,
    source ? h("select", { onchange: (e) => { state.source = e.target.value; store.set("source", e.target.value); route(); } },
      Object.entries(SOURCES).map(([v, l]) => h("option", { value: v, selected: v === state.source }, l))) : null);
}

// ---- pages --------------------------------------------------------------------------------------
async function overview() {
  const d = await api(`/api/overview?range=${state.range}&source=${state.source}`);
  const tokenView = d.usd_per_task;
  const hero = card(null, null, h("div", { class: "hero" },
    h("div", {}, h("div", { class: "fig" }, usd(d.usd_per_verified)),
      h("div", { class: "cap" }, "per verified outcome · API-equivalent, failed tasks included")),
    h("div", { class: "vs" }, "A token dashboard would show ", h("b", {}, `${usd(tokenView)} per task`),
      h("br"), `${d.verified} of ${d.tasks} tasks verified · ${d.failed} failed · ${d.unverified} unverified`)),
    h("div", { class: "tiles" },
      tile("Spend (API-equivalent)", usd(d.usd), `${num(d.tokens)} tokens · prices ${d.prices}`),
      tile("Spent on failed tasks", usd(d.wasted_usd), `${pct(d.wasted_share)} of spend`),
      tile("Spent on unverified tasks", usd(d.unverified_usd), "no test or grader ran"),
      tile("Energy per verified outcome", d.wh_per_verified == null ? "–" : wh(d.wh_per_verified),
        d.wh_per_verified == null ? "measured only for self-hosted models" : `measured (NVML) · ${d.measured_tasks} tasks`),
      tile("CO₂e per verified outcome", d.co2_g_per_verified == null ? "–" : `${d.co2_g_per_verified.toFixed(2)} g`,
        `grid factor ${d.co2_factor} kg/kWh (India)`),
      tile("Policy violations", `${d.violations}`, "consequential, in graded runs")));
  const days = h("div");
  const meters = h("div", { class: "meters" }, d.limits.length ? d.limits.map((l) => {
    const name = l.source.replace("claude_code:.", "Claude Code · ").replace("codex", "Codex");
    const win = l.window === "five_hour" ? "5-hour window" : "weekly";
    if (l.stale) return h("div", { class: "meter" },
      h("div", { class: "top" }, h("span", {}, `${name} · ${win}`), h("span", { class: "n muted" }, "reset since last reading")),
      h("div", { class: "bar" }, h("i", { style: "width:0%" })),
      h("div", { class: "when" }, `last seen ${l.used_pct.toFixed(0)}% on ${when(l.ts)}`));
    return h("div", { class: `meter ${l.used_pct >= 80 ? "warn" : ""}` },
      h("div", { class: "top" }, h("span", {}, `${name} · ${win}`), h("span", { class: "n" }, `${l.used_pct.toFixed(0)}% used${l.used_pct >= 80 ? " ⚠" : ""}`)),
      h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, l.used_pct)}%` })),
      h("div", { class: "when" }, l.resets_at ? `resets ${when(l.resets_at)}` : ""));
  }) : h("div", { class: "muted small" }, "No subscription limit readings yet."));
  const srcRows = Object.entries(d.by_source);
  const bySource = h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Source"), h("th", { class: "n" }, "Tasks"),
    h("th", { class: "n" }, "Verified"), h("th", { class: "n" }, "Spend"), h("th", { class: "n" }, "$ / verified"))),
    h("tbody", {}, srcRows.map(([k, v]) => h("tr", {}, h("td", {}, SOURCES[k] || k), h("td", { class: "n" }, v.tasks),
      h("td", { class: "n" }, v.verified), h("td", { class: "n" }, usd(v.usd)), h("td", { class: "n" }, v.verified ? usd(v.usd / v.verified) : "–")))));
  main.replaceChildren(filters(), hero, h("div", { class: "row two" },
    card("Spend by day", "API-equivalent $, split by how the task ended", days),
    h("div", {}, card("Usage limits", "Subscription windows, as the agents last reported them", meters),
      card("By source", null, srcRows.length ? bySource : h("div", { class: "muted small" }, "No tasks in this range.")))));
  stackedColumns(days, {
    data: d.days.map((x) => ({ x: x.day, label: x.day.slice(5), parts: x })),
    series: [{ key: "verified", label: "Verified", color: "--s1" }, { key: "failed", label: "Failed", color: "--s2" },
      { key: "unverified", label: "Unverified", color: "--other" }],
    fmt: usd,
  });
}

async function live() {
  let since = Date.now() / 1000 - 900;
  const body = h("tbody");
  const status = h("span", { class: "muted small" }, "");
  let paused = false;
  const pause = h("button", { class: "theme", onclick: () => { paused = !paused; pause.textContent = paused ? "Resume" : "Pause"; } }, "Pause");
  const runs = await api("/api/runs").catch(() => []);
  const runSel = h("select", {}, h("option", { value: "" }, "Replay a logged run…"),
    runs.map((r) => h("option", { value: r.run_id }, `${r.run_id}${r.condition ? ` (${r.condition})` : ""}`)));
  const speedSel = h("select", {}, [1, 4, 8, 16, 32].map((x) => h("option", { value: x, selected: x === 8 }, `${x}×`)));
  const replayBtn = h("button", { class: "theme", onclick: async () => {
    if (!runSel.value) return;
    await fetch(`/api/replay?run=${encodeURIComponent(runSel.value)}&speed=${speedSel.value}`, { method: "POST" });
    body.replaceChildren(); since = Date.now() / 1000 - 1;
  } }, "Replay");
  main.replaceChildren(card("Live", "Every model call as it is booked: which tier served it, why, and what it cost",
    h("div", { class: "filters" }, pause, runSel, speedSel, replayBtn, status),
    h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Time", "Run / source", "Task", "Role", "Tier", "Decision", "Tools", "Tokens in · cached · out", "Energy", "Cost"]
      .map((c, i) => h("th", { class: i >= 7 ? "n" : "" }, c)))), body)));
  const tick = async () => {
    if (paused) return;
    const d = await api(`/api/live?since=${since}`);
    const fresh = d.calls.filter((c) => c.ts_start > since);
    if (fresh.length) since = Math.max(...fresh.map((c) => c.ts_start));
    for (const c of fresh.reverse()) {
      const dec = decisionChip(c);
      body.prepend(h("tr", { class: "fresh click", onclick: () => c.task_key && go(`#/task/${encodeURIComponent(c.task_key)}`) },
        h("td", { class: "nw" }, clock(c.ts_start)), h("td", { class: "small nw" }, c.run_id === "observer" ? c.source : c.run_id),
        h("td", { class: "small" }, c.title ? c.title.slice(0, 48) : `task ${c.task_id ?? (c.task_key || "").split("/").pop()}`), h("td", {}, c.role),
        h("td", {}, c.tier ? h("span", { class: `chip ${c.tier}` }, c.tier) : (c.model || "")), h("td", {}, dec),
        h("td", { class: "small" }, (c.tool_names || []).join(", ")),
        h("td", { class: "n" }, `${num(c.input_tokens)} · ${num(c.cache_read_tokens)} · ${num(c.output_tokens)}`),
        h("td", { class: "n" }, c.energy_j ? `${c.energy_j.toFixed(0)} J` : "–"), h("td", { class: "n" }, usd(c.cost_usd))));
    }
    while (body.children.length > 200) body.lastChild.remove();
    status.textContent = `updated ${clock(d.now)} · ${body.children.length} calls shown`;
  };
  await tick();
  timer = setInterval(() => tick().catch(() => {}), 1500);
}

async function tasks() {
  const rows = await api(`/api/tasks?range=${state.range}&source=${state.source}&limit=400`);
  const tbl = h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Started", "Source / run", "Task", "Outcome", "Calls", "Tokens", "Energy", "Cost", "Violations"]
    .map((c, i) => h("th", { class: i >= 4 ? "n" : "" }, c)))),
    h("tbody", {}, rows.map((t) => h("tr", { class: "click", onclick: () => go(`#/task/${encodeURIComponent(t.task_key)}`) },
      h("td", { class: "small" }, when(t.started)), h("td", { class: "small" }, t.run_id === "observer" ? (SOURCES[t.source] || t.source) : t.run_id),
      h("td", {}, t.title || `${t.workload || "task"} · ${t.task_id}`), h("td", {}, outcome(t.outcome)), h("td", { class: "n" }, t.calls), h("td", { class: "n" }, num(t.tok)),
      h("td", { class: "n" }, t.j ? wh(t.j / 3600) : "–"), h("td", { class: "n" }, usd(t.usd)), h("td", { class: "n" }, t.violations ?? "–")))));
  main.replaceChildren(filters(), card("Tasks", `${rows.length} tasks · click one for its receipt`, rows.length ? tbl : h("div", { class: "empty" }, "No tasks in this range.")));
}

async function receipt(key) {
  const { task: t, calls } = await api(`/api/task?key=${encodeURIComponent(key)}`);
  const sum = (f) => calls.reduce((a, c) => a + (c[f] || 0), 0);
  const tin = sum("input_tokens") + sum("cache_read_tokens") + sum("cache_write_tokens");
  const usdTot = sum("cost_usd"), j = sum("energy_j");
  const tests = (t.meta.tests || []).map((x) => h("div", { class: "small" }, `${x.exit === 0 ? "✓" : "✗"} exit ${x.exit} · ${x.cmd}`));
  const policy = (t.meta.policy || []).map((x) => h("div", { class: "small" }, `${x.rule} at step ${x.step} (${x.tool})`));
  const table = h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Time", "Role", "Tier / model", "In · cached · out", "Tools", "Why", "Energy", "Cost"]
    .map((c, i) => h("th", { class: i === 3 || i >= 6 ? "n" : "" }, c)))),
    h("tbody", {}, calls.map((c) => h("tr", {}, h("td", { class: "small" }, clock(c.ts_start)), h("td", {}, c.role),
      h("td", { class: "small" }, c.tier ? h("span", { class: `chip ${c.tier}` }, c.tier) : "", " ", c.model || ""),
      h("td", { class: "n" }, `${num(c.input_tokens)} · ${num(c.cache_read_tokens)} · ${num(c.output_tokens)}`),
      h("td", { class: "small" }, (c.tool_names || []).join(", ")),
      h("td", { class: "small" }, decisionChip(c, true)),
      h("td", { class: "n" }, c.energy_j ? `${c.energy_j.toFixed(0)} J` : "–"), h("td", { class: "n" }, usd(c.cost_usd))))));
  main.replaceChildren(h("div", { class: "back", onclick: () => history.back() }, "← back"),
    card(null, null, h("div", { class: "receipt" },
      h("div", { class: "head" }, h("h3", {}, t.title || `${t.workload || "task"} · task ${t.task_id}`), outcome(t.outcome)),
      h("div", { class: "muted small" }, `${t.source}${t.run_id !== "observer" ? ` · run ${t.run_id}` : ""} · started ${when(t.started)}`),
      h("div", { class: "tiles" }, tile("Cost (API-equivalent)", usd(usdTot), `${calls.length} model calls`),
        tile("Tokens", num(tin + sum("output_tokens")), `${pct(tin ? sum("cache_read_tokens") / tin : 0)} cache hits`),
        tile("Energy", j ? wh(j / 3600) : "–", j ? "measured, time-share" : "not measured (API model)"),
        tile("Policy violations", t.violations ?? "–", t.violations == null ? "not checked" : "consequential")),
      tests.length ? h("div", {}, h("div", { class: "muted small" }, "Test runs (last 5)"), ...tests) : null,
      policy.length ? h("div", {}, h("div", { class: "muted small" }, "Policy findings"), ...policy) : null)),
    card("Calls", "Every model call booked to this task, in order", table));
}

async function workloadPicker(onPick) {
  const wls = await api("/api/workloads");
  if (!wls.length) return [null, h("div")];
  if (!state.workload || !wls.find((w) => w.workload === state.workload)) state.workload = wls[0].workload;
  const sel = h("select", { onchange: (e) => { state.workload = e.target.value; store.set("workload", e.target.value); onPick(); } },
    wls.map((w) => h("option", { value: w.workload, selected: w.workload === state.workload }, w.workload)));
  return [state.workload, h("div", { class: "filters" }, h("label", {}, "Workload"), sel)];
}

// An effect counts only if its 95% CI excludes zero AND it exceeds the measured baseline-vs-baseline noise floor.
let NOISE = null;
// Which part of the stack each condition puts under test: NVIDIA software is named where it is used.
const STACK = {
  baseline: "strong model every step (Qwen3.8-27B)", downgrade: "cheap model every step (Qwen3.5-9B)",
  nemotron: "NVIDIA Nemotron-3-Nano-30B-A3B every step", mask: "context trick: old tool outputs masked",
  router: "off-the-shelf router (von 1.3)", "sy-escalation": "NVIDIA NeMo Switchyard · escalation router",
  "sy-stage": "NVIDIA NeMo Switchyard · stage router (tuned for coding agents)",
  "sy-escalation-j27": "NVIDIA NeMo Switchyard · escalation router, 27B as judge",
  "nemotron-think": "NVIDIA Nemotron-3-Nano-30B-A3B as shipped (reasoning on)",
  "nemotron-think-nim": "NVIDIA Nemotron-3-Nano-30B-A3B as shipped, served by NVIDIA NIM",
  "gov-policy-a100": "TrueCost governor + policy check · gate off: 9B every step, writes pinned to the 27B and policy-checked",
};
const stackOf = (c) => STACK[c] || (/^gov-policy-a/.test(c) ? `TrueCost governor + policy check · risk budget α ${c.replace("gov-policy-a", "")}%`
  : /^gov-a/.test(c) ? `TrueCost governor · risk budget α ${c.replace("gov-a", "")}%` : null);
const co2 = (r) => r.compare.co2_g_per_verified_run == null ? "" : ` · ${r.compare.co2_g_per_verified_run.toFixed(2)} g CO₂ per verified (India grid, CEA 0.727 g/Wh)`;
function beyond(r) {
  const ci = r.compare.success_delta_ci95;
  if (!ci || ci[0] == null) return false;
  const excludes0 = ci[0] > 0 || ci[1] < 0;
  return excludes0 && (!NOISE || Math.abs(r.compare.success_delta_pp) > NOISE.mean_abs_delta_pp);
}

async function crashtest() {
  const [wl, picker] = await workloadPicker(crashtest);
  if (!wl) { main.replaceChildren(card("Crash-test", null, h("div", { class: "empty" }, "No crash-test runs in the ledger yet."))); return; }
  const d = await api(`/api/crashtest?workload=${encodeURIComponent(wl)}`);
  NOISE = d.noise_floor;
  const fam = ["reliability", "safety", "security", "sustainability", "cost"];
  const board = h("table", { class: "t board" }, h("thead", {}, h("tr", {}, h("th", {}, "Trick"), fam.map((f) => h("th", {}, f[0].toUpperCase() + f.slice(1))),
    h("th", { class: "n" }, "Verified"), h("th", { class: "n" }, "Δ vs baseline"), h("th", { class: "n", title: "share of tasks solved in all k runs" }, "pass^k"), h("th", { class: "n", title: "API-equivalent: tokens at public list prices" }, "$ / verified"), h("th", { class: "n", title: "What the GPU time cost: each call's share of the H100 (k calls in flight share it 1/k) × the hourly price Brev bills" }, "GPU bill / verified"), h("th", { class: "n" }, "Energy / verified"), h("th", { class: "n" }, "Violations / 100"))),
    h("tbody", {}, d.rows.map((r) => h("tr", { style: r.provisional ? "opacity:.55" : "" },
      h("td", { class: "trick" }, r.condition, r.condition === d.baseline && r.condition !== "baseline" ? h("span", { class: "muted small" }, "  (baseline)") : null,
        stackOf(r.condition) ? h("div", { class: "muted small" }, stackOf(r.condition)) : null,
        r.provisional ? h("div", { class: "muted small" }, `provisional · ${r.compare.runs || 1} run${(r.compare.runs || 1) > 1 ? "s" : ""}`) : null),
      fam.map((f) => h("td", {}, stars(r.stars[f]))),
      h("td", { class: "n" }, pct(r.compare.success_run)),
      h("td", { class: "n", title: `95% CI ${r.compare.success_delta_ci95 ? r.compare.success_delta_ci95.map((x) => x.toFixed(1)).join(" to ") : "–"} pp · lost ${r.compare.lost} · gained ${r.compare.gained} · p = ${r.compare.p_value?.toFixed(3)} (task-cluster) · ${r.compare.runs} run pair(s) on ${r.compare.tasks} tasks` },
        r.condition === d.baseline ? "–" : `${pp(r.compare.success_delta_pp)}${beyond(r) ? " *" : ""}`),
      h("td", { class: "n" }, r.consistency ? `${pct(r.consistency.pass_all)} (k=${r.consistency.k})` : "–"),
      h("td", { class: "n" }, `${usd(r.compare.usd_per_verified_run)} ${r.condition === d.baseline ? "" : ratio(r.compare.usd_per_verified_ratio)}`),
      h("td", { class: "n" }, r.compare.gpu_usd_per_verified_run == null ? "–" : `${usd(r.compare.gpu_usd_per_verified_run)} ${r.condition === d.baseline ? "" : ratio(r.compare.gpu_usd_per_verified_ratio)}`),
      h("td", { class: "n", title: `measured on the GPU (NVML)${co2(r)}` }, `${wh(r.compare.wh_per_verified_run)} ${r.condition === d.baseline ? "" : ratio(r.compare.energy_per_verified_ratio)}`),
      h("td", { class: "n" }, r.compare.violations_run_per_100 == null ? "–" : r.compare.violations_run_per_100.toFixed(1))))));
  const dots = h("div"), batch = h("div");
  main.replaceChildren(picker,
    card("Crash-test ratings", `What each cost-saving trick breaks, against the ${d.baseline || "baseline"} on paired tasks · stars per protocol ${d.protocol} (thresholds fixed before the runs) · * beyond noise: 95% CI excludes 0 and the effect exceeds the noise floor`,
      d.rows.length ? h("div", { class: "scroll" }, board) : h("div", { class: "empty" }, d.error || "Runs pending."),
      d.noise_floor ? h("p", { class: "note", style: "margin:12px 0 0" },
        `Noise floor: two runs of the unchanged baseline differ by ${d.noise_floor.mean_abs_delta_pp.toFixed(1)} pp on average `,
        `(up to ${d.noise_floor.max_abs_delta_pp.toFixed(1)} pp over ${d.noise_floor.pairs} pair(s)), so single-run comparisons of cost tricks can mislead.`) : null),
    card("What a token dashboard sees vs what TrueCost sees", "Change vs baseline: tokens per task, against $ and energy per verified outcome", dots),
    card("Why routing can raise energy", "Measured joules per model call on the strong tier's GPU, by how many calls it was batched with. Tricks that thin out a server's traffic pay this.", batch));
  api(`/api/batching?workload=${encodeURIComponent(wl)}&gpu=0`).then((rows) => groupedBars(batch, {
    rows: rows.map((r) => ({ label: `${r.in_flight} in flight`, values: { j: r.j_per_call } })),
    series: [{ key: "j", label: "Joules per call", color: "--s1" }], fmt: (v) => `${v.toFixed(0)} J`, rowLabel: "Calls in flight",
  })).catch(() => {});
  dotPlot(dots, {
    rows: d.rows.filter((r) => r.condition !== d.baseline).map((r) => ({ label: r.condition, values: {
      tok: r.compare.tokens_per_task_ratio == null ? null : 100 * (r.compare.tokens_per_task_ratio - 1),
      usd: r.compare.usd_per_verified_ratio == null ? null : 100 * (r.compare.usd_per_verified_ratio - 1),
      gpu: r.compare.gpu_usd_per_verified_ratio == null ? null : 100 * (r.compare.gpu_usd_per_verified_ratio - 1),
      j: r.compare.energy_per_verified_ratio == null ? null : 100 * (r.compare.energy_per_verified_ratio - 1) } })),
    series: [{ key: "tok", label: "Tokens per task (token dashboard)", short: "tokens", color: "--other" },
      { key: "usd", label: "$ per verified outcome (list price)", short: "$", color: "--s1" },
      { key: "gpu", label: "GPU bill per verified outcome", short: "GPU $", color: "--s4" },
      { key: "j", label: "Energy per verified outcome", short: "energy", color: "--s3" }],
  });
}

// The frozen gate each workload's governor ran with (evaluation/gates/); any other workload looks for <workload>.json.
const GATE_OF = { "tau2-retail": "retail", "ab-finance": "ab-think" };
const SWEEP = /^gov-a\d/;  // the governor alone at each α; the policy-check variants are plotted but not on the dial

async function dial() {
  const [wl, picker] = await workloadPicker(dial);
  if (!wl) { main.replaceChildren(card("Risk dial", null, h("div", { class: "empty" }, "No runs yet."))); return; }
  const d = await api(`/api/crashtest?workload=${encodeURIComponent(wl)}&subset=gov`);
  const gov = d.rows.filter((r) => SWEEP.test(r.condition)).map((r) => ({ ...r, alpha: +r.condition.match(/^gov-a(\d+(?:\.\d+)?)/)[1] }))
    .sort((a, b) => a.alpha - b.alpha);
  const sel = { i: Math.max(0, gov.findIndex((g) => g.alpha === 10)) };
  const chart = h("div"), info = h("div", { class: "note" });
  const aLabel = h("div", { class: "a" }, gov.length ? `${gov[sel.i].alpha}%` : "–");
  const slider = gov.length > 1 ? h("input", { type: "range", min: 0, max: gov.length - 1, step: 1, value: sel.i,
    oninput: (e) => { sel.i = +e.target.value; draw(); } }) : null;
  const draw = () => {
    const g = gov[sel.i];
    if (g) aLabel.textContent = `${g.alpha}%`;
    scatter(chart, {
      points: d.rows.map((r) => {
        const isGov = SWEEP.test(r.condition);
        return { x: 100 * (r.compare.success_run ?? 0), y: r.compare.usd_per_verified_run ?? 0, label: r.condition,
          group: r.condition === d.baseline ? "base" : isGov ? "gov" : /^gov/.test(r.condition) ? "policy" : "other",
          labelled: r.condition === d.baseline || !isGov, sel: g && r.condition === g.condition };
      }),
      groups: [{ key: "base", label: "Baseline (strong tier)", color: "--ink-2" }, { key: "gov", label: "TrueCost governor (α sweep)", color: "--s1" },
        { key: "policy", label: "Governor + policy check", color: "--s3" }, { key: "other", label: "Other cost-saving tricks", color: "--other" }],
      path: "gov", xlabel: "Verified success", ylabel: "$ per verified outcome", xfmt: (v) => `${v.toFixed(0)}%`, yfmt: usd,
    });
    info.replaceChildren(g ? h("div", {}, `At α = ${g.alpha}%: verified success ${pct(g.compare.success_run)} (${pp(g.compare.success_delta_pp)} vs baseline, `,
      `p = ${g.compare.p_value?.toFixed(2)}), $ per verified ${ratio(g.compare.usd_per_verified_ratio)}, energy per verified ${ratio(g.compare.energy_per_verified_ratio)}, `,
      `${pct(g.compare.cheap_call_share_run)} of model calls on the cheap tier.`) :
      h("div", { class: "muted" }, "Governor runs pending: the dial appears once the α sweep has run."));
  };
  const g = await api(`/api/gate?name=${encodeURIComponent(GATE_OF[wl] || wl)}`).catch(() => ({ missing: true }));
  const gateCard = g.missing ? card("Governor calibration", null, h("div", { class: "muted small" }, "No calibrated gate yet: it is fitted from the baseline's record-mode twins.")) :
    card("Governor calibration", `Gate: P(cheap draft matches the strong model's action), fitted on train-split tasks; thresholds by conformal risk control (task level). Updated ${when(g.updated)}.`,
      h("div", { class: "tiles" },
        tile("Steps observed", num(g.data.steps), `${pct(g.data.pinned_share)} write drafts pinned (not gated)`),
        tile("Cheap draft = strong action", pct(g.data.match_rate), "on gated steps"),
        tile("Gate AUC", g.data.auc_cal?.toFixed(3) ?? "–", `on ${g.data.cal_units} held-out calibration tasks`),
        tile("Calibration error (ECE)", (g.data.ece_cal_platt_crossfit ?? g.data.ece_cal)?.toFixed(3) ?? "–",
          `target ≤ 0.05 · raw ${g.data.ece_cal?.toFixed(3)}; Platt-recalibrated, cross-fitted (decisions unchanged)`),
        ...(g.data.writes ? [tile("Pinned writes", `${g.data.writes.cheap_wrong_write + g.data.writes.cheap_writes_where_strong_did_not} wrong writes stopped`,
          `of ${g.data.writes.strong_writes} strong-model writes, the cheap draft got ${g.data.writes.cheap_wrong_write} wrong and wrote ${g.data.writes.cheap_writes_where_strong_did_not} times when the strong model did not`)] : []),
        ...(g.ablation && g.ablation.draft_only ? (() => {
          const auc = (x) => (x && (x.auc_cal ?? x.auc_fit_inner)) ?? null;
          const f = (v) => v == null ? "–" : v.toFixed(2);
          return [tile("Ablation AUC", `${f(auc(g.ablation.draft_only))} / ${f(auc(g.ablation.decision_model_only))}`,
            `draft confidence only / decision model (von) only${g.ablation.draft_only.auc_fit_inner != null ? ", inner folds of the fitting tasks" : ""}; chosen: ${g.ablation.chosen || "–"}`)];
        })() : [])),
      h("table", { class: "t", style: "margin-top:12px" }, h("thead", {}, h("tr", {}, ["α (risk budget)", "Threshold", "Gated steps accepted", "Calibration risk", "Feasible"]
        .map((c, i) => h("th", { class: i ? "n" : "" }, c)))),
        h("tbody", {}, (g.report || []).map((r) => h("tr", {}, h("td", {}, `${(100 * r.alpha).toFixed(0)}%`),
          h("td", { class: "n" }, r.threshold > 1 ? "never accept" : r.threshold.toFixed(3)), h("td", { class: "n" }, pct(r.accept_rate_gated)),
          h("td", { class: "n" }, r.calibration_risk.toFixed(3)), h("td", { class: "n" }, r.feasible ? "yes" : "needs more data"))))));
  main.replaceChildren(picker, card("Risk dial", `Declare how many extra failures you accept (α); the governor takes savings only inside it. Each blue point is a held-out run at one α${d.subset_tasks ? `; every point is measured on the same ${d.subset_tasks} held-out tasks` : ""}.`,
    h("div", { class: "dial" }, h("label", { class: "muted" }, "α"), slider, aLabel), chart, info), gateCard);
  draw();
}

async function attacks() {
  const [wl, picker] = await workloadPicker(attacks);
  if (!wl) { main.replaceChildren(card("Attacks", null, h("div", { class: "empty" }, "No runs yet."))); return; }
  const d = await api(`/api/attacks?workload=${encodeURIComponent(wl)}`);
  const chart = h("div");
  const conds = [...new Set(d.rows.map((r) => r.condition))];
  // the unprotected baseline in grey, the TrueCost governor in blue, every other condition its own colour
  const others = ["--s2", "--s4", "--s3"];
  let k = 0;
  const colorOf = (c) => c === "baseline" ? "--other" : /^gov/.test(c) ? "--s1" : others[k++ % others.length];
  main.replaceChildren(picker, card("Attacks", "Cost amplification under attack (attacked $ per task ÷ clean), per condition. TrueCost's target is at most 1.5×.", chart));
  const attacksList = [...new Set(d.rows.map((r) => r.attack))];
  groupedBars(chart, {
    rows: attacksList.map((a) => ({ label: a, values: Object.fromEntries(d.rows.filter((r) => r.attack === a).map((r) => [r.condition, r.amplification])) })),
    series: conds.map((c) => ({ key: c, label: c, color: colorOf(c) })), fmt: (v) => `×${v.toFixed(2)}`, ref: 1.5, refLabel: "target 1.5×",
    empty: "Attack runs pending.",
  });
}

// ---- plans: API price vs subscription usage ------------------------------------------------------
async function plans() {
  const d = await api("/api/plans");
  if (!d.agents.length) { main.replaceChildren(card("Plans", null, h("div", { class: "empty" }, "No subscription runs in the ledger yet."))); return; }
  const win = d.window === "five_hour" ? "5-hour window" : "weekly window";
  const pctf = (v, dd = 2) => v == null ? "–" : `${v.toFixed(dd)}%`;
  const cards = [];
  for (const a of d.agents) {
    const runs = a.runs.filter((r) => r.v);
    for (const r of runs) r.resolved = r.plan_pct_per_pass != null && r.lo > 0;  // interval excludes 0
    const base = Object.fromEntries(runs.filter((r) => r.condition === "baseline").map((r) => [r.tests, r]));
    const rows = runs.sort((x, y) => (x.tests > y.tests ? -1 : x.tests < y.tests ? 1 : 0) || (y.usd_per_pass || 0) - (x.usd_per_pass || 0));
    const table = h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Config"), h("th", {}, "Model · effort"), h("th", {}, "Tests"), h("th", { class: "n" }, "Passed"),
        h("th", { class: "n" }, "API $ / pass"), h("th", { class: "n", title: "90% interval in brackets" }, `% of ${win} / pass`), h("th", { class: "n" }, "Passes per window"))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        h("td", {}, r.condition, r.seeds > 1 ? h("span", { class: "muted small" }, `  ${r.seeds} seeds`) : null), h("td", {}, `${r.model} · ${r.effort || "–"}`), h("td", {}, r.tests),
        h("td", { class: "n" }, `${r.v}/${r.n}`), h("td", { class: "n" }, usd(r.usd_per_pass)),
        h("td", { class: "n" }, r.plan_pct_per_pass == null ? "–" : `${pctf(r.plan_pct_per_pass)} `, r.lo == null ? null : h("span", { class: "muted small" }, `(${r.lo.toFixed(2)}–${r.hi.toFixed(2)})`)),
        h("td", { class: "n" }, r.resolved ? `≈${Math.round(100 / r.plan_pct_per_pass)}` : h("span", { class: "muted small" }, r.plan_pct_per_pass == null ? "–" : "too few readings"))))));
    const dots = h("div");
    const unit = a.basis === "usd" ? "API $" : "million tokens";
    const meter = h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", { class: "n" }, `${unit} observed`), h("th", { class: "n" }, `% of ${win} per ${unit}`), h("th", { class: "n" }, "90% interval"))),
      h("tbody", {}, a.models.map((m) => h("tr", {}, h("td", {}, m.model), h("td", { class: "n" }, a.basis === "usd" ? usd(m.spent) : m.spent.toFixed(2)),
        h("td", { class: "n" }, m.k == null ? "n/a" : m.k.toFixed(1)),
        h("td", { class: "n muted" }, m.k == null ? `seen in ${m.blocks} blocks` : `${m.lo.toFixed(1)}–${m.hi.toFixed(1)}`)))));
    cards.push(
      card(`${a.agent}: what one passed exercise costs`, `API $ is the same work on a pay-per-token bill. Plan % is how much of the subscription's ${win} it used, fitted from the live usage readings in the agent's own logs (${a.intervals} intervals pooled in blocks of ${a.block}; the fit explains ${a.explained_pct.toFixed(0)} of ${a.delta_pct.toFixed(0)} points of window movement${a.delta_pct < 20 ? "; too early, intervals are wide" : ""}). Tests hidden = the agent implements from the instructions alone.`,
        rows.length ? table : h("div", { class: "empty" }, "Runs pending.")),
      card(`${a.agent}: cheaper on the bill is not cheaper on the plan`, "Change vs the baseline config with the same tests, per passed exercise", dots),
      card(`${a.agent}: how the plan meters each model`, a.basis === "usd"
        ? `% of the ${win} per API-equivalent $. Equal numbers would mean the plan charges by API price.`
        : `% of the ${win} per million tokens (input, cache reads and output): tokens explain the meter better than API $ here, so the plan does not follow the price list, which charges models up to 100× apart and discounts cache reads 10×.`,
        a.models.length ? meter : h("div", { class: "empty" }, "Not enough usage readings yet.")));
    dotPlot(dots, {
      rows: rows.filter((r) => r.condition !== "baseline" && base[r.tests]).map((r) => {
        const b = base[r.tests];
        return { label: `${r.condition} (${r.tests})`, values: {
          api: b.usd_per_pass ? 100 * (r.usd_per_pass / b.usd_per_pass - 1) : null,
          plan: b.resolved && r.resolved ? 100 * (r.plan_pct_per_pass / b.plan_pct_per_pass - 1) : null } };
      }),
      series: [{ key: "api", label: "API $ per pass", short: "API", color: "--s1" },
        { key: "plan", label: `Plan % per pass (${win})`, short: "plan", color: "--s3" }],
    });
  }
  main.replaceChildren(...cards);
}

// ---- router -------------------------------------------------------------------------------------
const PAGES = { overview, live, tasks, crashtest, dial, attacks, plans };
function go(hash) { location.hash = hash; }
async function route() {
  clearInterval(timer);
  const [, page = "overview", arg] = location.hash.split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.dataset.page === (page === "task" ? "tasks" : page)));
  try {
    if (page === "task") await receipt(decodeURIComponent(arg || ""));
    else await (PAGES[page] || overview)();
  } catch (e) {
    main.replaceChildren(card("Something went wrong", null, h("div", { class: "muted" }, String(e))));
  }
}
document.querySelector("button.theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || "auto";
  const next = { auto: "light", light: "dark", dark: "auto" }[cur];
  if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
  store.set("theme", next);
  document.querySelector("button.theme").textContent = `Theme: ${next}`;
  route();
});
const savedTheme = store.get("theme", "auto");
if (savedTheme !== "auto") document.documentElement.dataset.theme = savedTheme;
document.querySelector("button.theme").textContent = `Theme: ${savedTheme}`;
addEventListener("hashchange", route);
route();
