#!/usr/bin/env bash
# Nemotron as shipped, served by NVIDIA NIM instead of SGLang: stops the Qwen servers, starts the NIM container on
# :30013 (the port the nemotron tiers use), then the 9B (the simulated customer) next to it, runs the cell
# nemotron-think-nim for $SEEDS, and restores the h100 layout however it ends. Run on a host with no grid running.
# The NGC key is read from $NGC_API_KEY_FILE (default ~/.config/truecost/ngc_api_key), never from the repo.
# Usage: SEEDS="303 304 305" bash evaluation/serving/nim_nemotron.sh
set -uo pipefail
D=$(cd "$(dirname "$0")" && pwd); TC=$(cd "$D/../.." && pwd); WORK=${WORK:-$HOME/truecost-work}
PY=${PY:-$TC/.venv/bin/python}; IMG=${NIM_IMAGE:-nvcr.io/nim/nvidia/nemotron-3-nano:2.1.4}
KEY=${NGC_API_KEY_FILE:-$HOME/.config/truecost/ngc_api_key}; mkdir -p "$WORK/logs"
log() { echo "$(date '+%F %T') [nim] $*" >> "$WORK/logs/master.log"; }
restore() { docker rm -f nim-nemotron >/dev/null 2>&1; bash "$D/tiers.sh" up h100 >> "$WORK/logs/master.log" 2>&1 || log "restoring h100 failed"; }
trap restore EXIT
cd "$TC"
log "NIM: stopping the Qwen servers; image $IMG"
bash "$D/tiers.sh" down
# NIM starts first on an empty GPU, with ~55% of it and the FP8 profile pinned by id (the checkpoint the SGLang row
# serves). NIM's vLLM needs a tool parser and the reasoning parser shipped in the model's snapshot to serve agent
# tool calls, and one Mamba cache block per sequence, hence --max-num-seqs 16 (the SGLang row's request cap).
mkdir -p ~/.cache/nim
docker rm -f nim-nemotron >/dev/null 2>&1
NGC_API_KEY=$(cat "$KEY") docker run -d --name nim-nemotron --gpus all --shm-size 16g -e NGC_API_KEY \
  -e NIM_MODEL_PROFILE=8c91cce84b9b032ff4af489cb1a20395e223af35623010df9155390ab2284b7a -v ~/.cache/nim:/opt/nim/.cache -p 127.0.0.1:30013:8000 "$IMG" \
  --gpu-memory-utilization 0.55 --max-model-len 131072 --served-model-name nemotron-3-nano-30b \
  --max-num-seqs 16 --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nano_v3 \
  --reasoning-parser-plugin /opt/nim/.cache/ngc/hub/models--nim--nvidia--nemotron-3-nano/snapshots/hf-fp8-f25a17a/nano_v3_reasoning_parser.py \
  >> "$WORK/logs/nim.log" 2>&1
for i in $(seq 1 720); do  # up to an hour on a cold cache; stop waiting as soon as the container dies
  curl -sf 127.0.0.1:30013/v1/health/ready >/dev/null && break
  [ -z "$(docker ps -q -f name=nim-nemotron -f status=running)" ] && break
  sleep 5
done
curl -sf 127.0.0.1:30013/v1/health/ready >/dev/null || { log "NIM not ready"; docker logs nim-nemotron 2>&1 | tail -150 >> "$WORK/logs/nim.log"; exit 1; }
log "NIM ready; starting the 9B next to it"
bash "$D/tiers.sh" up h100-nim-sim >> "$WORK/logs/master.log" 2>&1 || { log "9B did not start next to NIM"; exit 1; }
for pass in 1 2; do "$PY" -m truecost.cli crashtest evaluation/grids/retail-nvidia.toml --only nemotron-think-nim --seeds ${SEEDS:-303 304 305} >> "$WORK/logs/master.log" 2>&1; done
log "nemotron-think-nim (${SEEDS:-303 304 305}) done"
