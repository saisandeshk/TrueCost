#!/usr/bin/env bash
# Serve one model tier with SGLang (venv: $WORK/venv-sgl, made by setup.sh).
# Usage: launch_tier.sh <gpu> <port> <model_dir> <served_name> <mem_fraction> [extra sglang args...]
# --enable-cache-report puts cached prompt tokens in every response, so the ledger can price cache hits.
set -euo pipefail
GPU=$1; PORT=$2; MODEL=$3; NAME=$4; MEM=$5; shift 5
WORK=${WORK:-$HOME/truecost-work}; V=$WORK/venv-sgl
SP=$V/lib/python3.11/site-packages/nvidia
LIBS=""
for d in cu13 cublas cuda_cupti cuda_nvrtc cuda_runtime cudnn cufft cufile curand cusolver cusparse cusparselt nccl nvjitlink nvshmem nvtx; do LIBS="$LIBS:$SP/$d/lib"; done
export LD_LIBRARY_PATH="${LIBS#:}:/usr/local/cuda/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"
export PATH="$V/bin:$HOME/.local/bin:/usr/local/cuda/bin:$PATH"  # $V/bin: ninja for SGLang's JIT kernels
export CUDA_VISIBLE_DEVICES=$GPU
exec $V/bin/python -m sglang.launch_server --model-path "$MODEL" \
  --served-model-name "$NAME" --host 127.0.0.1 --port "$PORT" --tp-size 1 \
  --context-length 32768 --mem-fraction-static "$MEM" --trust-remote-code \
  --attention-backend flashinfer --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
  --enable-metrics --enable-cache-report "$@"
