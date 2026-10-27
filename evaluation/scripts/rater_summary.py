"""Every rater number on results/README.md, from the published rater verdicts (release asset rater-retail.jsonl.xz)
and the results ledger: agreement with the rule checker (Cohen's κ, per rule), and per condition the share of
held-out tasks with a consequential violation by each. The held-out tasks are the ones the governor ran on.
    python evaluation/scripts/rater_summary.py rater-retail.jsonl [~/.truecost/results.db] > results/rater-summary.json
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from second_rater import CONSEQUENTIAL, summarize  # noqa: E402

rated = Path(sys.argv[1]).expanduser()
db = Path(sys.argv[2] if len(sys.argv) > 2 else "~/.truecost/results.db").expanduser()
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
held = {r[0] for r in con.execute("SELECT DISTINCT task_id FROM tasks WHERE run_id LIKE 'retail-gov-a20-s%'")}
cons = set(CONSEQUENTIAL)
by = defaultdict(lambda: {"tasks": 0, "checker": 0, "rater": 0})
for line in rated.read_text().splitlines():
    r = json.loads(line) if line.strip() else None
    if not r or not r["rater_ok"] or str(r["task"]) not in held:
        continue
    c = by[r["run"].rsplit("-s", 1)[0].removeprefix("retail-")]
    c["tasks"] += 1
    c["checker"] += any(set(w["rule_checker"]) & cons for w in r["writes"])
    c["rater"] += any(set(w["rater"]) & cons for w in r["writes"])
out = {"agreement": summarize(rated), "held_out_tasks": len(held),
       "held_out_violation_share": {k: {"tasks": v["tasks"], "checker_pct": round(100 * v["checker"] / v["tasks"], 1),
                                        "rater_pct": round(100 * v["rater"] / v["tasks"], 1)}
                                    for k, v in sorted(by.items())}}
print(json.dumps(out, indent=1))
