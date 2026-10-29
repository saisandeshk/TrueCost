// Answers the console's /api/* calls from the pre-computed files in data/, so the unmodified console runs on a
// static host. Every filter is the "all time" view of the snapshot; Live replays a logged run in the browser.
(() => {
  const realFetch = window.fetch.bind(window);
  const BASE = new URL(".", document.baseURI).href;
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const file = (p) => realFetch(BASE + "data/" + p);
  // FNV-1a over the key's UTF-8 bytes; the build names each receipt file the same way
  const fnv = (s) => { let x = 0x811c9dc5; for (const b of new TextEncoder().encode(s)) { x ^= b; x = Math.imul(x, 0x01000193) >>> 0; } return x.toString(16).padStart(8, "0"); };
  const slug = (s) => encodeURIComponent(s).replace(/%/g, "~");
  try { localStorage.setItem("tc.range", "all"); } catch { /* private mode */ }

  const replays = {};
  let rp = null, firstVisit = true;
  async function startReplay(run, speed) {
    replays[run] = replays[run] || await (await file(`replay/${slug(run)}.json`)).json();
    const calls = replays[run];
    rp = { run, speed: Math.max(0.1, speed), start: Date.now() / 1000, t0: calls[0].ts_start, t1: calls[calls.length - 1].ts_start, calls };
    return { run, speed: rp.speed, start: rp.start, t0: rp.t0, t1: rp.t1, duration_s: (rp.t1 - rp.t0) / rp.speed };
  }
  async function live(since) {
    const now = Date.now() / 1000;
    if (!rp && firstVisit) { firstVisit = false; await startReplay(window.TRUECOST_SNAPSHOT.replay_default, window.TRUECOST_SNAPSHOT.replay_speed); }
    if (!rp) return { now, calls: [] };
    const horizon = rp.t0 + (now - rp.start) * rp.speed;
    const out = [];
    for (const c of rp.calls) {
      if (c.ts_start > horizon) break;
      const t = rp.start + (c.ts_start - rp.t0) / rp.speed;  // re-timed onto the wall clock, as the server does
      if (t > since) out.push({ ...c, ts_start: t, replay: true });
    }
    if (horizon > rp.t1 + 5) rp = null;  // finished: the run plays once
    return { now, calls: out.reverse().slice(0, 300) };
  }

  window.fetch = async (input, init = {}) => {
    const u = new URL(typeof input === "string" ? input : input.url, location.href);
    const i = u.pathname.indexOf("/api/");
    if (i < 0) return realFetch(input, init);
    const ep = u.pathname.slice(i + 5), q = u.searchParams, method = (init.method || "GET").toUpperCase();
    const src = q.get("source") || "all", wl = q.get("workload") || "";
    switch (ep) {
      case "defaults": return json({ range: "all", source: "everything" });
      case "overview": return file(`overview/${slug(src)}.json`);
      case "models": return file(`models/${slug(src)}.json`);
      case "tasks": return file(`tasks/${slug(src)}.json`);
      case "task": return file(`task/${fnv(q.get("key") || "")}.json`);
      case "runs": return file("runs.json");
      case "workloads": return file("workloads.json");
      case "plans": return file("plans.json");
      case "crashtest": return file(`crashtest/${slug(wl)}${q.get("subset") === "gov" ? ".gov" : ""}.json`);
      case "attacks": return file(`attacks/${slug(wl)}.json`);
      case "batching": return file(`batching/${slug(wl)}.json`);
      case "gate": return file(`gate/${slug(q.get("name") || "retail")}.json`);
      case "live": return json(await live(+q.get("since") || Date.now() / 1000 - 600));
      case "replay":
        if (method === "DELETE") { rp = null; return json({ ok: true }); }
        return json(await startReplay(q.get("run"), +q.get("speed") || 8));
      default: return json({ detail: "not in this snapshot" }, 404);
    }
  };
})();
