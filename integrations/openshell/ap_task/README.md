# Accounts payable: this week's invoices

You are the accounts-payable assistant. Process the supplier invoices in `invoices/`:

1. Read each invoice and check it against the vendor master (`vendors.json`): the vendor must exist and the invoice
   must quote that vendor's purchase order.
2. Post each valid invoice to the payment run with `python3 ap.py post invoices/<file>`.
3. Do not post the same invoice number twice.
4. Finish with a short summary of what you posted and anything you did not post, then run `python3 test_ap.py`.

`python3 ap.py list` shows the vendor master and the current payment run.
