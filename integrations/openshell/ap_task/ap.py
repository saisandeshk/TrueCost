"""A tiny accounts-payable system for the TrueCost × OpenShell demo: a vendor master and a payment run.

  python3 ap.py list                       vendors and the payment run
  python3 ap.py post invoices/<file>       add an invoice to the payment run (paid to the vendor's IBAN on file)
  python3 ap.py update-bank <vendor> <iban>  change a vendor's bank account (sensitive: audited)
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else []


def save(name, data):
    (HERE / name).write_text(json.dumps(data, indent=2) + "\n")


def audit(event):
    with open(HERE / "audit.log", "a") as f:
        f.write(json.dumps(event) + "\n")


def field(text, name):
    m = re.search(rf"^{name}:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else None


def post(path):
    text = (HERE / path).read_text() if not Path(path).is_absolute() else Path(path).read_text()
    vendors = {v["id"]: v for v in load("vendors.json")}
    vid, number, po = field(text, "Vendor ID"), field(text, "Invoice number"), field(text, "PO")
    amount = field(text, "Total due")
    if vid not in vendors:
        sys.exit(f"error: unknown vendor {vid!r}")
    if po != vendors[vid]["po"]:
        sys.exit(f"error: PO {po!r} does not match the vendor's purchase order {vendors[vid]['po']!r}")
    cents = round(float(re.sub(r"[^0-9.]", "", amount or "")) * 100)
    run = load("payment_run.json")
    run.append({"invoice": number, "vendor": vid, "amount_cents": cents, "iban": vendors[vid]["iban"]})
    save("payment_run.json", run)
    audit({"event": "post", "invoice": number, "vendor": vid, "amount_cents": cents})
    print(f"posted {number}: {vendors[vid]['name']}, {cents / 100:.2f} EUR to {vendors[vid]['iban']}")


def update_bank(vid, iban):
    vendors = load("vendors.json")
    for v in vendors:
        if v["id"] == vid:
            audit({"event": "bank_change", "vendor": vid, "old": v["iban"], "new": iban})
            v["iban"] = iban
            save("vendors.json", vendors)
            print(f"bank account of {vid} changed to {iban}")
            return
    sys.exit(f"error: unknown vendor {vid!r}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["list"]:
        print(json.dumps({"vendors": load("vendors.json"), "payment_run": load("payment_run.json")}, indent=2))
    elif a[:1] == ["post"] and len(a) == 2:
        post(a[1])
    elif a[:1] == ["update-bank"] and len(a) == 3:
        update_bank(a[1], a[2])
    else:
        sys.exit(__doc__)
