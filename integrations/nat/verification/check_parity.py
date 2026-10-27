"""Compare NAT artifacts with core reporting on the same ledger, opened read-only."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path

from truecost import cli
from truecost.report import compare, run_metrics
from nat_truecost._ledger import ReadOnlyLedger, read_ledger

HERE = Path(__file__).resolve().parent
LEDGER = Path.home() / ".truecost/results.db"
RUN, BASE = "retail-downgrade-s303", "retail-baseline-s303"
summary = json.loads((HERE.parent / "output/tau2_retail/truecost_summary.json").read_text())
with read_ledger(LEDGER) as db:
    expected = json.loads(json.dumps(run_metrics(db, RUN)))
    paired = compare(db, BASE, RUN)
assert {key: summary[key] for key in expected} == expected
assert summary["comparison"] == json.loads(json.dumps(paired))

# cmd_report is unchanged. Only replace its write-initializing ledger factory for this
# check: use the exact same source file in mode=ro, including the JSON/text CLI formatters.
connections = []
def readonly_factory(path):
    db = ReadOnlyLedger(path)
    connections.append(db)
    return db

original = cli._ledger
try:
    cli._ledger = readonly_factory
    for as_json, filename in ((False, "core-report.txt"), (True, "core-report.json")):
        stream = io.StringIO()
        with redirect_stdout(stream):
            argv = ["--db", str(LEDGER), "report", RUN, "--baseline", BASE]
            cli.main(argv + (["--json"] if as_json else []))
        (HERE / filename).write_text(stream.getvalue())
finally:
    cli._ledger = original
    for db in connections:
        db.close()
report = json.loads((HERE / "core-report.json").read_text())
assert report["runs"][0] == expected
assert report["comparisons"][0] == summary["comparison"]

before = json.loads((HERE / "ledger-before.json").read_text())
after = {p.name: {"size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
         for p in LEDGER.parent.glob(LEDGER.name + "*")}
(HERE / "ledger-after.json").write_text(json.dumps(after, indent=2) + "\n")
# In WAL mode even a read-only reader creates the -shm index and an empty -wal; the data lives in the main file.
assert before[LEDGER.name] == after[LEDGER.name], "ledger database changed"
assert after.get(LEDGER.name + "-wal", {"size": 0})["size"] == 0, "uncheckpointed writes in the WAL"
print(f"PASS: {len(expected)} run fields and {len(paired)} comparison fields match core reporting exactly.")
print("PASS: ledger database size and SHA-256 unchanged; WAL empty (read-only access).")
print(json.dumps({k: expected[k] for k in ("tasks", "verified", "usd_per_verified", "energy_wh_per_verified",
                                         "success_rate", "violations_per_100", "wasted_cost_share")}, indent=2))
