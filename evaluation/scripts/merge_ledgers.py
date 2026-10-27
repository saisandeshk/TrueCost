"""Merge several ledgers into one analysis ledger (e.g. several GPU hosts, each holding its own seeds).

The console pairs a condition with the baseline of the same seed inside ONE ledger, so seeds spread over several
ledgers must be merged first. Inputs are opened read-only; rows are keyed by call/task/run id, so re-merging is safe.
Usage: python evaluation/scripts/merge_ledgers.py OUT.db IN1.db [IN2.db ...]
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from truecost.ledger import Ledger  # noqa: E402

TABLES = ("runs", "tasks", "calls", "energy", "limits")


def main(out: str, *ins: str) -> None:
    tmp = Path(out).with_suffix(".tmp.db")
    tmp.unlink(missing_ok=True)
    Ledger(tmp)  # creates the schema
    con = sqlite3.connect(f"file:{tmp}", uri=True)  # URI mode, so the inputs can be attached read-only
    for i, src in enumerate(ins):
        con.execute(f"ATTACH DATABASE ? AS s{i}", (f"file:{Path(src).expanduser()}?mode=ro",))
        for t in TABLES:
            cols = [r[1] for r in con.execute(f"PRAGMA s{i}.table_info({t})")]
            if cols:
                con.execute(f"INSERT OR REPLACE INTO main.{t} ({','.join(cols)}) SELECT {','.join(cols)} FROM s{i}.{t}")
        con.commit()
        con.execute(f"DETACH DATABASE s{i}")
    n = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    con.close()
    tmp.replace(out)
    print(f"merged {len(ins)} ledgers -> {out}: {n}")


if __name__ == "__main__":
    main(*sys.argv[1:])
