"""A small accounts-payable agent for the quickstart: it totals supplier invoices (quantities, unit prices, a discount
and tax) through the TrueCost gateway, and grades each answer to the cent. Every call carries its task id, and every
graded answer is recorded as the task's outcome, so `truecost report` prices the run per verified task.

    python examples/quickstart/invoice_agent.py --run qs-strong --model strong
    python examples/quickstart/invoice_agent.py --run qs-cheap --model cheap
    truecost --db ~/.truecost/quickstart.db report qs-cheap --baseline qs-strong
"""
import argparse
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal

import httpx

from truecost import cli

ITEMS = ["steel brackets", "M8 bolts (box)", "safety gloves", "pallet wrap", "LED panels", "cable trays",
         "hydraulic hose", "label printer rolls", "forklift battery service", "warehouse racking beams"]


def invoice(i: int) -> tuple[str, str]:
    """A seeded invoice and its exact total."""
    rnd = random.Random(1000 + i)
    lines = [(rnd.choice(ITEMS), rnd.randint(1, 48), Decimal(rnd.randint(150, 98000)) / 100) for _ in range(rnd.randint(3, 6))]
    discount, tax = rnd.choice([0, 2, 5, 7.5, 10]), rnd.choice([5, 12, 18])
    sub = sum(q * p for _, q, p in lines)
    after = sub * (1 - Decimal(str(discount)) / 100)
    total = (after * (1 + Decimal(tax) / 100)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    text = "\n".join(f"- {name}: {q} × {p:.2f}" for name, q, p in lines)
    prompt = (f"Supplier invoice INV-{3100 + i}\n{text}\nTrade discount: {discount}% on the subtotal.\n"
              f"Tax: {tax}% on the discounted subtotal.\n\nCompute the amount payable. Reply with JSON only: "
              '{"total": "<amount with 2 decimals>"}')
    return prompt, f"{total:.2f}"


def run_task(gw: str, run: str, model: str, i: int) -> bool:
    prompt, expected = invoice(i)
    headers = {"X-TrueCost-Run": run, "X-TrueCost-Task": f"inv-{i:02d}"}
    messages = [{"role": "system", "content": "You are an accounts-payable assistant. Be exact."},
                {"role": "user", "content": prompt}]
    answer = None
    for _ in range(2):  # one retry when the reply is not the JSON asked for
        r = httpx.post(f"{gw}/chat/completions", headers=headers, timeout=300,
                       json={"model": model, "messages": messages, "max_tokens": 4096, "temperature": 0})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{[^{}]*\"total\"[^{}]*\}", text)
        if m:
            try:
                answer = str(json.loads(m.group(0))["total"]).replace(",", "").strip()
                break
            except (ValueError, KeyError):
                pass
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": 'Reply with JSON only: {"total": "<amount with 2 decimals>"}'}]
    ok = answer is not None and re.fullmatch(r"-?\d+(\.\d+)?", answer) is not None \
        and Decimal(answer).quantize(Decimal("0.01")) == Decimal(expected)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:30020/v1")
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default="strong", help="the gateway tier to ask for: strong or cheap")
    ap.add_argument("--tasks", type=int, default=20)
    ap.add_argument("--db", default="~/.truecost/quickstart.db", help="the gateway's ledger (outcomes go here)")
    a = ap.parse_args()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: run_task(a.gateway, a.run, a.model, i), range(a.tasks)))
    for i, ok in enumerate(results):  # the grader's verdict becomes the task's outcome
        cli.main(["--db", a.db, "outcome", a.run, f"inv-{i:02d}", "verified" if ok else "failed"])
    print(f"{a.run}: {sum(results)}/{len(results)} invoices totalled exactly")


if __name__ == "__main__":
    main()
