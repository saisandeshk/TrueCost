"""A deterministic train/test split for a workload that ships none (about 60% training, 40% held
out). Tasks are ranked by SHA-1 of salt and id; the lowest-ranked 40% (rounded) are held out. It depends only on the
ids and the salt, so anyone can recompute it, and the shares are exact. The output has the shape of τ²'s split_tasks.json, so `truecost calibrate --split FILE:train` and grids
(`task_ids`) can use it. Usage: python evaluation/scripts/make_split.py --out evaluation/grids/splits/ab-finance.json ID [ID ...]
"""
import argparse
import hashlib
import json
from pathlib import Path


def rank(task_id: str, salt: str) -> int:
    return int(hashlib.sha1(f"{salt}:{task_id}".encode()).hexdigest(), 16)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--salt", default="truecost-split")
    ap.add_argument("--test-share", type=float, default=0.4)
    a = ap.parse_args()
    held = set(sorted(a.ids, key=lambda t: rank(t, a.salt))[:round(a.test_share * len(a.ids))])
    split = {"train": [t for t in a.ids if t not in held], "test": [t for t in a.ids if t in held]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(split, indent=1) + "\n")
    print(f"{a.out}: train {len(split['train'])}, test {len(split['test'])}")


if __name__ == "__main__":
    main()
