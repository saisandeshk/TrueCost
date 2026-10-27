#!/usr/bin/env bash
# Reproduce the plugin's acceptance checks: plugin tests, the API audit against the installed NAT, an offline
# `nat eval` over a finished retail run, and exact parity with `truecost report` on the same read-only ledger.
# Needs the results ledger (`truecost demo --download-only`) and the plugin venv (see ../README.md).
set -euo pipefail
cd "$(dirname "$0")/../../.."
PY=integrations/nat/.venv/bin/python; V=integrations/nat/verification; LEDGER=~/.truecost/results.db
$PY -m pytest -q integrations/nat/tests | tee $V/plugin-tests.txt
$PY $V/audit_api.py | tee $V/api-audit.txt
$PY - "$LEDGER" > $V/ledger-before.json <<'PY'
import hashlib, json, sys
from pathlib import Path
p = Path(sys.argv[1]).expanduser()
print(json.dumps({f.name: {"size": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
                  for f in p.parent.glob(p.name + "*")}, indent=2))
PY
NAT_TELEMETRY_ENABLED=false integrations/nat/.venv/bin/nat eval --config_file integrations/nat/configs/tau2_retail_eval.yml > $V/nat-eval.txt 2>&1
$PY $V/check_parity.py | tee $V/parity.txt
