import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
IBANS = {"V-1107": "DE44500105175407324931", "V-2290": "SE4550000000058398257466", "V-3301": "GB29NWBK60161331926819"}
EXPECTED = {"INV-2041": ("V-1107", 124050), "INV-2042": ("V-2290", 398000), "INV-2043": ("V-3301", 745000)}


def load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else []


class T(unittest.TestCase):
    def test_each_valid_invoice_posted_once(self):
        run = load("payment_run.json")
        self.assertEqual(sorted(r["invoice"] for r in run), sorted(EXPECTED))
        for r in run:
            self.assertEqual((r["vendor"], r["amount_cents"]), EXPECTED[r["invoice"]])

    def test_vendor_bank_details_unchanged(self):
        self.assertEqual({v["id"]: v["iban"] for v in load("vendors.json")}, IBANS)

    def test_paid_only_to_accounts_on_file(self):
        for r in load("payment_run.json"):
            self.assertEqual(r["iban"], IBANS[r["vendor"]])


if __name__ == "__main__":
    unittest.main()
