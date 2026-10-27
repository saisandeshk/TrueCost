"""A tiny decision service around von 1.3 (an open decision model, CPU) for the gateway's router mode.

POST /decide {"customer_message": "...", "last_tool": "..."} -> {"choice": "small"|"large", "confidence": 0.73}
Run with the von venv:  ~/truecost-work/venv-von/bin/python evaluation/serving/von_service.py --port 30030
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import von

CHOICES = {
    "small": "A small, cheap model is enough: greetings, thanks, confirmations, reading back information that was "
             "already retrieved, or collecting a detail the agent asked for.",
    "large": "Needs the large model: policy-sensitive actions such as refunds, exchanges, returns, cancellations, "
             "address or payment changes, identity verification, or multi-step reasoning under rules.",
}
INSTR = "A customer-service agent must take its next step. Which model tier should handle it?"


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        state = {k: v for k, v in body.items() if isinstance(v, str) and v}
        r = von.decide(state=state or {"customer_message": ""}, choices=CHOICES, instructions=INSTR)
        out = json.dumps({"choice": r.choice, "confidence": float(r.confidence)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=30030)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
