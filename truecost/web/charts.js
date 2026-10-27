// Small SVG chart kit for the console. Rules: thin marks (bars <= 24px, 4px rounded data end, 2px surface
// gaps), hairline solid grid, text in ink tokens (never series colors), a hover tooltip on every mark,
// a legend for >= 2 series, and a table view for every chart. All text goes in via text nodes.

const NS = "http://www.w3.org/2000/svg";

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

function s(tag, attrs = {}, text) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  if (text != null) el.append(document.createTextNode(String(text)));
  return el;
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function ticks(min, max, n = 4) {
  const out = [];
  const step = (max - min) / n;
  for (let i = 0; i <= n; i++) out.push(min + step * i);
  return out;
}

function tooltip(wrap) {
  const tip = h("div", { class: "tip" });
  wrap.append(tip);
  return {
    show(ev, rows) {
      tip.replaceChildren(...rows);
      tip.style.display = "block";
      const r = wrap.getBoundingClientRect();
      let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 10;
      if (x + tip.offsetWidth > r.width) x = ev.clientX - r.left - tip.offsetWidth - 14;
      tip.style.left = `${x}px`;
      tip.style.top = `${y}px`;
    },
    hide() { tip.style.display = "none"; },
  };
}

function tableToggle(wrap, head, rows) {
  const tbl = h("table", { class: "t", style: "display:none;margin-top:10px" },
    h("thead", {}, h("tr", {}, head.map((c, i) => h("th", { class: i ? "n" : "" }, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => h("td", { class: i ? "n" : "" }, c))))));
  const btn = h("button", { class: "toggle-table", onclick: () => {
    const on = tbl.style.display === "none";
    tbl.style.display = on ? "" : "none";
    btn.textContent = on ? "Hide table" : "Show table";
  } }, "Show table");
  wrap.append(h("div", { style: "margin-top:6px" }, btn), tbl);
}

function widthOf(el, min = 520) {
  return Math.max(min, Math.round(el.getBoundingClientRect().width || 760));
}

function legend(series, shape = "rect") {
  if (series.length < 2) return h("div");  // a single series needs no legend: the title names it
  return h("div", { class: "legend" }, series.map((sr) =>
    h("span", {}, h("i", { class: shape === "dot" ? "dot" : "", style: `background:${css(sr.color)}` }), sr.label)));
}

// Stacked columns over time: data [{x, parts: {key: value}}], series [{key, label, color}].
export function stackedColumns(el, { data, series, fmt = (v) => v, height = 220 }) {
  el.replaceChildren();
  const wrap = h("div", { class: "chart" });
  el.append(legend(series), wrap);
  if (!data.length) { wrap.append(h("div", { class: "empty" }, "No data in this range yet.")); return; }
  const W = widthOf(el), H = height, L = 52, R = 8, T = 8, B = 26;
  const totals = data.map((d) => series.reduce((a, sr) => a + (d.parts[sr.key] || 0), 0));
  const ymax = niceMax(Math.max(...totals));
  const band = (W - L - R) / data.length;
  const bw = Math.min(24, band * 0.6);
  const y = (v) => T + (H - T - B) * (1 - v / ymax);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const g = s("g", { class: "grid" });
  for (const t of ticks(0, ymax)) {
    g.append(s("line", { x1: L, x2: W - R, y1: y(t), y2: y(t) }));
    svg.append(s("text", { x: L - 8, y: y(t) + 4, "text-anchor": "end" }, fmt(t)));
  }
  svg.prepend(g);
  svg.append(s("line", { class: "base", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
  const tip = tooltip(wrap);
  const every = Math.ceil(data.length / 8);
  data.forEach((d, i) => {
    const cx = L + band * i + band / 2;
    let acc = 0;
    const segs = series.filter((sr) => (d.parts[sr.key] || 0) > 0);
    segs.forEach((sr, j) => {
      const v = d.parts[sr.key];
      const y0 = y(acc), y1 = y(acc + v);
      acc += v;
      const top = j === segs.length - 1;
      const hgt = Math.max(0, y0 - y1 - (j ? 2 : 0));
      const yy = y1;
      if (top && hgt > 4) {
        const r = 4, x0 = cx - bw / 2;
        svg.append(s("path", { fill: css(sr.color), d:
          `M${x0},${yy + hgt} V${yy + r} Q${x0},${yy} ${x0 + r},${yy} H${x0 + bw - r} Q${x0 + bw},${yy} ${x0 + bw},${yy + r} V${yy + hgt} Z` }));
      } else {
        svg.append(s("rect", { x: cx - bw / 2, y: yy, width: bw, height: hgt, fill: css(sr.color) }));
      }
    });
    if (i % every === 0) svg.append(s("text", { x: cx, y: H - 6, "text-anchor": "middle" }, d.label || d.x));
    const hit = s("rect", { x: cx - band / 2, y: T, width: band, height: H - T - B, fill: "transparent", tabindex: 0 });
    const rows = () => [h("div", {}, h("b", {}, fmt(totals[i])), "  ", d.label || d.x),
      ...series.map((sr) => h("div", {}, h("span", { class: "k", style: `background:${css(sr.color)}` }),
        `${sr.label}: ${fmt(d.parts[sr.key] || 0)}`))];
    hit.addEventListener("pointermove", (ev) => tip.show(ev, rows()));
    hit.addEventListener("pointerleave", () => tip.hide());
    svg.append(hit);
  });
  wrap.append(svg);
  tableToggle(el, ["Day", ...series.map((sr) => sr.label), "Total"],
    data.map((d, i) => [d.label || d.x, ...series.map((sr) => fmt(d.parts[sr.key] || 0)), fmt(totals[i])]));
}

// Diverging dot plot (dumbbell): rows [{label, values: {key: pct}}], series [{key, label, color}].
// Values are % change vs baseline; the zero line is the baseline.
export function dotPlot(el, { rows, series, fmt = (v) => `${v > 0 ? "+" : ""}${v.toFixed(0)}%`, note }) {
  el.replaceChildren();
  const wrap = h("div", { class: "chart" });
  el.append(legend(series, "dot"), wrap);
  if (!rows.length) { wrap.append(h("div", { class: "empty" }, "Runs pending.")); return; }
  const W = widthOf(el), rowH = 34, L = 150, R = 250, T = 26;
  const H = T + rows.length * rowH + 24;
  const vals = rows.flatMap((r) => series.map((sr) => r.values[sr.key]).filter((v) => v != null));
  const m = niceMax(Math.max(10, ...vals.map(Math.abs)));
  const x = (v) => L + (W - L - R) * ((v + m) / (2 * m));
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const g = s("g", { class: "grid" });
  for (const t of ticks(-m, m, 4)) {
    g.append(s("line", { x1: x(t), x2: x(t), y1: T - 6, y2: H - 22 }));
    svg.append(s("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, fmt(t)));
  }
  svg.append(g, s("line", { class: "base", x1: x(0), x2: x(0), y1: T - 10, y2: H - 22 }));
  svg.append(s("text", { x: x(0), y: T - 14, "text-anchor": "middle", class: "ink" }, note || "baseline"));
  const tip = tooltip(wrap);
  rows.forEach((r, i) => {
    const cy = T + rowH * i + rowH / 2;
    svg.append(s("text", { x: L - 12, y: cy + 4, "text-anchor": "end", class: "ink" }, r.label));
    const pts = series.map((sr) => [sr, r.values[sr.key]]).filter(([, v]) => v != null);
    if (pts.length === 2) svg.append(s("line", { x1: x(pts[0][1]), x2: x(pts[1][1]), y1: cy, y2: cy, stroke: css("--axis"), "stroke-width": 2 }));
    for (const [sr, v] of pts) {
      svg.append(s("circle", { cx: x(v), cy, r: 6, fill: css(sr.color), stroke: css("--surface"), "stroke-width": 2 }));
    }
    if (pts.length) svg.append(s("text", { x: W - R + 16, y: cy + 4, class: "ink" },
      pts.map(([sr, v]) => `${sr.short || sr.label} ${fmt(v)}`).join(" · ")));
    const hit = s("rect", { x: L, y: cy - rowH / 2, width: W - L - R, height: rowH, fill: "transparent", tabindex: 0 });
    hit.addEventListener("pointermove", (ev) => tip.show(ev, [h("div", {}, h("b", {}, r.label)),
      ...pts.map(([sr, v]) => h("div", {}, h("span", { class: "k", style: `background:${css(sr.color)}` }), `${sr.label}: ${fmt(v)}`))]));
    hit.addEventListener("pointerleave", () => tip.hide());
    svg.append(hit);
  });
  wrap.append(svg);
  tableToggle(el, ["Condition", ...series.map((sr) => sr.label)],
    rows.map((r) => [r.label, ...series.map((sr) => (r.values[sr.key] == null ? "–" : fmt(r.values[sr.key])))]));
}

// Frontier scatter: points [{x, y, label, group, sel}], groups [{key, label, color}], optional path (group key).
export function scatter(el, { points, groups, path, xlabel, ylabel, xfmt, yfmt, height = 300 }) {
  el.replaceChildren();
  const wrap = h("div", { class: "chart" });
  el.append(legend(groups, "dot"), wrap);
  if (!points.length) { wrap.append(h("div", { class: "empty" }, "Runs pending.")); return; }
  const W = widthOf(el), H = height, L = 64, R = 24, T = 14, B = 40;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const x0 = Math.max(0, Math.floor((Math.min(...xs) - 5) / 5) * 5), x1 = Math.min(100, Math.ceil((Math.max(...xs) + 5) / 5) * 5);
  const y1 = niceMax(Math.max(...ys) * 1.1);
  const x = (v) => L + (W - L - R) * ((v - x0) / (x1 - x0 || 1));
  const y = (v) => T + (H - T - B) * (1 - v / y1);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const g = s("g", { class: "grid" });
  for (const t of ticks(0, y1)) {
    g.append(s("line", { x1: L, x2: W - R, y1: y(t), y2: y(t) }));
    svg.append(s("text", { x: L - 8, y: y(t) + 4, "text-anchor": "end" }, yfmt(t)));
  }
  for (const t of ticks(x0, x1, 5)) svg.append(s("text", { x: x(t), y: H - B + 18, "text-anchor": "middle" }, xfmt(t)));
  svg.prepend(g);
  svg.append(s("line", { class: "base", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
  svg.append(s("text", { x: (L + W - R) / 2, y: H - 4, "text-anchor": "middle", class: "ink" }, xlabel));
  svg.append(s("text", { x: 14, y: T + (H - T - B) / 2, "text-anchor": "middle", class: "ink",
    transform: `rotate(-90 14 ${T + (H - T - B) / 2})` }, ylabel));
  const color = Object.fromEntries(groups.map((gr) => [gr.key, css(gr.color)]));
  if (path) {
    const pp = points.filter((p) => p.group === path).sort((a, b) => a.x - b.x);
    if (pp.length > 1) svg.append(s("path", { d: pp.map((p, i) => `${i ? "L" : "M"}${x(p.x)},${y(p.y)}`).join(" "),
      fill: "none", stroke: color[path], "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }
  const tip = tooltip(wrap);
  for (const p of points) {
    const c = s("circle", { cx: x(p.x), cy: y(p.y), r: p.sel ? 8 : 5, fill: color[p.group], stroke: css("--surface"), "stroke-width": 2 });
    svg.append(c);
    if (p.labelled || p.sel) svg.append(s("text", { x: x(p.x) + 10, y: y(p.y) - 8, class: "ink" }, p.label));
    const hit = s("circle", { cx: x(p.x), cy: y(p.y), r: 14, fill: "transparent", tabindex: 0 });
    hit.addEventListener("pointermove", (ev) => tip.show(ev, [h("div", {}, h("b", {}, yfmt(p.y)), "  ", p.label),
      h("div", {}, `${xlabel}: ${xfmt(p.x)}`)]));
    hit.addEventListener("pointerleave", () => tip.hide());
    svg.append(hit);
  }
  wrap.append(svg);
  tableToggle(el, ["Condition", xlabel, ylabel], points.map((p) => [p.label, xfmt(p.x), yfmt(p.y)]));
}

// Grouped horizontal bars: rows [{label, values: {key: v}}], series [{key,label,color}], optional ref line.
export function groupedBars(el, { rows, series, fmt, ref, refLabel, empty = "Runs pending." }) {
  el.replaceChildren();
  const wrap = h("div", { class: "chart" });
  el.append(legend(series), wrap);
  if (!rows.length) { wrap.append(h("div", { class: "empty" }, empty)); return; }
  const bh = 12, gap = 2, group = series.length * (bh + gap) + 14;
  const W = widthOf(el), L = 150, R = 60, T = 16;
  const H = T + rows.length * group + 24;
  const vmax = niceMax(Math.max(ref || 0, ...rows.flatMap((r) => series.map((sr) => r.values[sr.key] || 0))));
  const x = (v) => L + (W - L - R) * (v / vmax);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const g = s("g", { class: "grid" });
  for (const t of ticks(0, vmax)) {
    g.append(s("line", { x1: x(t), x2: x(t), y1: T - 4, y2: H - 22 }));
    svg.append(s("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, fmt(t)));
  }
  svg.append(g, s("line", { class: "base", x1: x(0), x2: x(0), y1: T - 4, y2: H - 22 }));
  if (ref) {
    svg.append(s("line", { x1: x(ref), x2: x(ref), y1: T - 8, y2: H - 22, stroke: css("--ink-2"), "stroke-width": 1 }));
    svg.append(s("text", { x: x(ref) + 4, y: T - 2, class: "ink" }, refLabel || ""));
  }
  const tip = tooltip(wrap);
  rows.forEach((r, i) => {
    const gy = T + group * i;
    svg.append(s("text", { x: L - 12, y: gy + (series.length * (bh + gap)) / 2 + 4, "text-anchor": "end", class: "ink" }, r.label));
    series.forEach((sr, j) => {
      const v = r.values[sr.key];
      if (v == null) return;
      const yy = gy + j * (bh + gap), x1 = x(v), r4 = Math.min(4, (x1 - L) / 2);
      svg.append(s("path", { fill: css(sr.color), d:
        `M${L},${yy} H${x1 - r4} Q${x1},${yy} ${x1},${yy + r4} V${yy + bh - r4} Q${x1},${yy + bh} ${x1 - r4},${yy + bh} H${L} Z` }));
      svg.append(s("text", { x: x1 + 6, y: yy + bh - 2, class: "ink" }, fmt(v)));
      const hit = s("rect", { x: L, y: yy - 1, width: W - L - R, height: bh + 2, fill: "transparent", tabindex: 0 });
      hit.addEventListener("pointermove", (ev) => tip.show(ev, [h("div", {}, h("b", {}, fmt(v)), "  ", r.label),
        h("div", {}, h("span", { class: "k", style: `background:${css(sr.color)}` }), sr.label)]));
      hit.addEventListener("pointerleave", () => tip.hide());
      svg.append(hit);
    });
  });
  wrap.append(svg);
  tableToggle(el, ["Attack", ...series.map((sr) => sr.label)],
    rows.map((r) => [r.label, ...series.map((sr) => (r.values[sr.key] == null ? "–" : fmt(r.values[sr.key])))]));
}
