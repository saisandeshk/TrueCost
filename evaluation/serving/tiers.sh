#!/usr/bin/env bash
# Start or stop a model-tier layout on one 80 GB H100. Each server runs in its own tmux session (tc-t-<name>) and
# restarts itself if it dies. Usage: tiers.sh up <layout> | down | status
#   h100           strong Qwen3.8-27B INT4 (:30011) and Qwen3.5-9B AWQ (:30012: the cheap tier, the simulated
#                  customer and Switchyard's judge), 128k context so long finance conversations fit
#   h100-nemotron  Nemotron-3-Nano-30B-A3B FP8 on SGLang (:30013) in place of the 27B; `up h100` swaps back
#   h100-nim-sim   only the 9B, next to NVIDIA NIM serving Nemotron on :30013 (started by nim_nemotron.sh)
set -uo pipefail
WORK=${WORK:-$HOME/truecost-work}; L=$(cd "$(dirname "$0")" && pwd)/launch_tier.sh; LOG=$WORK/logs
mkdir -p "$LOG"
snap() { ls -d "$WORK"/hf/models--$1/snapshots/*/ 2>/dev/null | head -1; }
M27=$(snap RedHatAI--Qwen3.8-27B-INT4); M9Q=$(snap QuantTrio--Qwen3.5-9B-AWQ)
start() {  # name gpu port model served mem [extra]
  local n=$1; shift
  tmux kill-session -t tc-t-$n 2>/dev/null
  tmux new-session -d -s tc-t-$n "while true; do WORK=$WORK $L $* >> $LOG/tier-$n.log 2>&1; echo \"\$(date) server exited; restarting\" >> $LOG/tier-$n.log; sleep 15; done"
}
wait_up() { for p in "$@"; do for i in $(seq 1 ${WAIT_TRIES:-240}); do curl -sf 127.0.0.1:$p/health >/dev/null && break; sleep 5; done
            curl -sf 127.0.0.1:$p/health >/dev/null || { echo "port $p not healthy"; return 1; }; done; }
stop_all() { for t in nemotron cheap strong; do tmux kill-session -t tc-t-$t 2>/dev/null; done; sleep 20; }
case "${1:-}" in
  up)
    case "${2:-h100}" in
      h100)
        # The 27B starts alone on an empty GPU; the 9B starts once it is healthy, so the 27B keeps ~18 GB of working
        # memory. Request caps stay modest (8 agents run at once) and prefill CUDA graphs are off (slow to capture).
        stop_all
        start strong 0 30011 "$M27" qwen3.8-27b 0.45 --context-length 131072 --mamba-ssm-dtype bfloat16 \
          --mamba-radix-cache-strategy extra_buffer_lazy --max-running-requests 16 --cuda-graph-max-bs-decode 16 \
          --disable-prefill-cuda-graph --chunked-prefill-size 4096
        wait_up 30011 || exit 1
        # SGLang's fraction counts memory other processes already hold: 0.70 = "all but 30% of the GPU"
        start cheap 0 30012 "$M9Q" qwen3.5-9b-awq 0.70 --dtype bfloat16 --context-length 131072 \
          --max-running-requests 32 --cuda-graph-max-bs-decode 32 --disable-prefill-cuda-graph
        wait_up 30011 30012 ;;
      h100-nemotron)
        tmux kill-session -t tc-t-strong 2>/dev/null; sleep 15
        start nemotron 0 30013 "$(snap nvidia--NVIDIA-Nemotron-3-Nano-30B-A3B-FP8)" nemotron-3-nano-30b 0.85 \
          --context-length 131072 --max-running-requests 16 --cuda-graph-max-bs-decode 16 --disable-prefill-cuda-graph \
          --reasoning-parser nemotron_3
        # the first start JIT-compiles the FP8 MoE kernels, which can take over 20 minutes
        WAIT_TRIES=720 wait_up 30013 ;;
      h100-nim-sim)
        # NIM holds ~55% of the GPU; the 9B takes "all but 15%" (SGLang counts NIM's memory as used)
        stop_all
        start cheap 0 30012 "$M9Q" qwen3.5-9b-awq 0.85 --dtype bfloat16 --context-length 131072 \
          --max-running-requests 16 --cuda-graph-max-bs-decode 16 --disable-prefill-cuda-graph
        wait_up 30012 ;;
      *) echo "unknown layout"; exit 2 ;;
    esac ;;
  down) for s in $(tmux ls -F '#S' 2>/dev/null | grep '^tc-t-'); do tmux kill-session -t $s; done; sleep 10 ;;
  status) tmux ls 2>/dev/null | grep tc-t-; nvidia-smi --query-gpu=index,memory.used --format=csv,noheader ;;
  *) echo "usage: tiers.sh up <layout>|down|status"; exit 2 ;;
esac
