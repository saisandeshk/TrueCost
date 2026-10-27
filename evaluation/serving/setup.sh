#!/usr/bin/env bash
# Set up one GPU host (NVIDIA Brev 1× H100 80 GB in our runs) to reproduce the evaluation. Idempotent: re-running
# skips what is already there. Everything except this repo goes under $WORK (default ~/truecost-work).
#   bash evaluation/serving/setup.sh [--with-von] [--with-nvidia]
#     --with-von     von 1.3 decision model for the "router" cell (CPU)
#     --with-nvidia  Nemotron weights, NeMo Switchyard, OpenShell, and an nvcr.io login for NIM
#                    (reads the NGC key from $NGC_API_KEY_FILE, default ~/.config/truecost/ngc_api_key)
set -euo pipefail
TC=$(cd "$(dirname "$0")/../.." && pwd); WORK=${WORK:-$HOME/truecost-work}
TAU2_COMMIT=5bfa7e37b36656b37dc6d022156be6563c1007f3
AB_COMMIT=4a8e1061254004d9dac807054eed33fad7d1ff14
SGLANG=0.5.20
WITH_VON=0; WITH_NVIDIA=0
for a in "$@"; do case $a in --with-von) WITH_VON=1 ;; --with-nvidia) WITH_NVIDIA=1 ;; esac; done
mkdir -p "$WORK/logs" "$WORK/hf"
step() { echo; echo "== $(date '+%T') $*"; }

step "GPU check"
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader
command -v uv >/dev/null || { step "installing uv"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH=$HOME/.local/bin:$PATH; }

step "TrueCost ($TC/.venv)"
[ -x "$TC/.venv/bin/python" ] || uv venv -p 3.11 "$TC/.venv"
uv pip install -p "$TC/.venv/bin/python" -e "$TC[gpu,dev]"
(cd "$TC" && .venv/bin/python -m pytest -q tests | tail -1)
"$TC/.venv/bin/python" - <<'PY'
import pynvml as n  # the same library and call TrueCost's energy meter uses
n.nvmlInit()
try:
    e = n.nvmlDeviceGetTotalEnergyConsumption(n.nvmlDeviceGetHandleByIndex(0))
    print(f"NVML energy counter: available ({e / 3.6e6:.3f} kWh since driver load): energy will be measured")
except n.NVMLError as err:
    print(f"NVML energy counter: not available ({err}): energy will be marked unknown")
PY

step "SGLang $SGLANG ($WORK/venv-sgl)"
[ -x "$WORK/venv-sgl/bin/python" ] || uv venv -p 3.11 "$WORK/venv-sgl"
uv pip install -p "$WORK/venv-sgl/bin/python" "sglang[all]==$SGLANG" huggingface_hub

step "models ($WORK/hf): strong Qwen3.8-27B INT4, cheap / simulated customer Qwen3.5-9B AWQ"
for m in RedHatAI/Qwen3.8-27B-INT4 QuantTrio/Qwen3.5-9B-AWQ; do
  "$WORK/venv-sgl/bin/python" -c "from huggingface_hub import snapshot_download as s; s('$m', cache_dir='$WORK/hf')"
done

step "τ²-bench at $TAU2_COMMIT ($WORK/tau2-bench, venv $WORK/venv-tau2)"
[ -d "$WORK/tau2-bench/.git" ] || git clone https://github.com/sierra-research/tau2-bench "$WORK/tau2-bench"
git -C "$WORK/tau2-bench" checkout -q $TAU2_COMMIT
[ -x "$WORK/venv-tau2/bin/python" ] || uv venv -p 3.12 "$WORK/venv-tau2"
uv pip install -p "$WORK/venv-tau2/bin/python" -e "$WORK/tau2-bench"
uv pip install -p "$WORK/venv-tau2/bin/python" -r "$TC/evaluation/serving/tau2-requirements.txt"  # pinned versions

step "AutomationBench at $AB_COMMIT ($WORK/automationbench)"
[ -d "$WORK/automationbench/.git" ] || git clone https://github.com/zapier/AutomationBench.git "$WORK/automationbench"
git -C "$WORK/automationbench" checkout -q $AB_COMMIT
(cd "$WORK/automationbench" && uv sync -q)

if [ $WITH_VON = 1 ]; then
  step "von 1.3 decision model ($WORK/venv-von, CPU)"
  [ -x "$WORK/venv-von/bin/python" ] || uv venv -p 3.12 "$WORK/venv-von"   # von-sdk needs Python >= 3.12
  uv pip install -p "$WORK/venv-von/bin/python" torch --index-url https://download.pytorch.org/whl/cpu
  uv pip install -p "$WORK/venv-von/bin/python" "von-sdk>=1.3.7"   # imports as `von`; not the unrelated PyPI "von"
  HF_HOME=$WORK/hf-von "$WORK/venv-von/bin/python" -c "from huggingface_hub import snapshot_download as s; s('wfzyx/von', cache_dir='$WORK/hf-von/hub')"
fi

if [ $WITH_NVIDIA = 1 ]; then
  step "Nemotron-3-Nano-30B-A3B FP8 weights"
  "$WORK/venv-sgl/bin/python" -c "from huggingface_hub import snapshot_download as s; s('nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8', cache_dir='$WORK/hf')"

  step "NeMo Switchyard 0.3"
  command -v ~/.cargo/bin/switchyard-server >/dev/null || {
    curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.96.1 --profile minimal
    . "$HOME/.cargo/env"
    cargo install switchyard-server --locked || cargo install --git https://github.com/NVIDIA-NeMo/Switchyard --tag v0.3.0 switchyard-server --locked
  }
  ~/.cargo/bin/switchyard-server --config "$TC/evaluation/configs/switchyard.toml" --dry-run

  step "OpenShell (docker driver) and the demo agent image"
  command -v openshell >/dev/null || curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
  export PATH=$HOME/.local/bin:$PATH
  mkdir -p ~/.config/openshell && echo "OPENSHELL_COMPUTE_DRIVER=docker" > ~/.config/openshell/gateway.env
  command -v setfacl >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q acl >/dev/null
  sudo setfacl -m u:"$USER":rw /var/run/docker.sock   # the OpenShell gateway runs as you and needs the docker socket
  systemctl --user restart openshell-gateway; sleep 5
  openshell status || true
  docker build -q -t truecost/opencode-agent:0.1 -f "$TC/integrations/openshell/Dockerfile" "$TC/integrations/openshell"

  step "nvcr.io login for NIM"
  KEY=${NGC_API_KEY_FILE:-$HOME/.config/truecost/ngc_api_key}
  if [ -f "$KEY" ]; then docker login nvcr.io -u '$oauthtoken' --password-stdin < "$KEY"; else echo "no NGC key at $KEY: skipping"; fi
fi

step "done"
cat <<EOT
Next:
  bash evaluation/serving/tiers.sh up h100          # the model tiers
  SEEDS="303" bash evaluation/run_boards.sh retail  # one retail seed of every cell
EOT
