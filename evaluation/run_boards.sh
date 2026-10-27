#!/usr/bin/env bash
# Run every cell of one board for the given seeds on this host, in the order the protocol fixes. Resumable: finished
# runs are skipped, and a second pass re-runs only what failed. Each seed runs entirely on one host, so every pair
# shares hardware. The model tiers must be up (serving/tiers.sh up h100).
#   SEEDS="303 304" bash evaluation/run_boards.sh retail     # τ²-bench retail (board seeds 303–310)
#   SEEDS="321 322 323" bash evaluation/run_boards.sh finance # AutomationBench finance (board seeds 321–323)
# NVIDIA rows that swap the GPU's models run last: NEMOTRON=1 adds Nemotron on SGLang; Nemotron on NIM has its own
# script (serving/nim_nemotron.sh). Progress goes to $WORK/logs/master.log.
set -uo pipefail
TC=$(cd "$(dirname "$0")/.." && pwd); WORK=${WORK:-$HOME/truecost-work}; PY=${PY:-$TC/.venv/bin/python}
BOARD=${1:?usage: run_boards.sh retail|finance}; SEEDS=${SEEDS:?set SEEDS}
mkdir -p "$WORK/logs"; cd "$TC"
log() { echo "$(date '+%F %T') [$BOARD] $*" >> "$WORK/logs/master.log"; }
ct() {
  "$PY" -m truecost.cli crashtest "$@" >> "$WORK/logs/master.log" 2>&1
  if ls "$WORK"/grids/*/STOPPED 2>/dev/null | grep -q .; then log "halting: a grid stopped (an upstream is down)"; exit 1; fi
}
service() {  # name port command...
  local n=$1 p=$2; shift 2
  tmux kill-session -t tc-$n 2>/dev/null; tmux new-session -d -s tc-$n "$* >> $WORK/logs/$n.log 2>&1"
  for i in $(seq 1 30); do curl -sf 127.0.0.1:$p/health >/dev/null && return 0; sleep 2; done
  log "$n not healthy"; exit 1
}
service switchyard 30031 "$HOME/.cargo/bin/switchyard-server --config $TC/evaluation/configs/switchyard.toml --port 30031 --routing-log-file $WORK/logs/switchyard-routing.jsonl"
[ -x "$WORK/venv-von/bin/python" ] && { tmux kill-session -t tc-von 2>/dev/null
  tmux new-session -d -s tc-von "HF_HOME=$WORK/hf-von OMP_NUM_THREADS=8 $WORK/venv-von/bin/python $TC/evaluation/serving/von_service.py --port 30030 >> $WORK/logs/von.log 2>&1"; }
G=evaluation/grids
case $BOARD in
  retail)
    for pass in 1 2; do
      for s in $SEEDS; do
        ct $G/retail-main.toml --only baseline downgrade mask router --seeds $s
        ct $G/retail-nvidia.toml --only sy-escalation sy-escalation-j27 sy-stage --seeds $s
        ct $G/retail-gov.toml --only gov-a5 gov-a10 gov-a20 gov-policy-a20 gov-policy-a100 --seeds $s
        ct $G/retail-attacks.toml --only router@ps gov-a10@ps baseline@runaway-paged gov-a10@runaway-paged --seeds $s
        ct $G/retail-nvidia-attacks.toml --seeds $s
        log "seed $s done"
      done
    done
    if [ "${NEMOTRON:-0}" = 1 ]; then
      restore() { bash "$TC/evaluation/serving/tiers.sh" up h100 >> "$WORK/logs/master.log" 2>&1 || log "restoring h100 failed"; }
      trap restore EXIT
      bash "$TC/evaluation/serving/tiers.sh" up h100-nemotron >> "$WORK/logs/master.log" 2>&1 || { log "nemotron layout failed"; exit 1; }
      for pass in 1 2; do ct $G/retail-nvidia.toml --only nemotron nemotron-think --seeds $SEEDS; done
      log "nemotron ($SEEDS) done"
    fi ;;
  finance)
    # The finance board's strong tier is the 27B with thinking on (configs/h100-think.toml, set in the grids).
    for pass in 1 2; do for s in $SEEDS; do ct $G/ab-board.toml --only baseline --seeds $s; done; done
    for pass in 1 2; do
      for s in $SEEDS; do
        ct $G/ab-board.toml --only downgrade sy-escalation sy-stage router mask --seeds $s
        ct $G/ab-gov.toml --only router@ps sy-escalation@ps baseline@runaway-paged --seeds $s
        ct $G/ab-gov.toml --only gov-a10 gov-a20 gov-a10@ps gov-a10@runaway-paged --seeds $s
        log "seed $s done"
      done
    done ;;
  *) echo "unknown board $BOARD"; exit 2 ;;
esac
log "board $BOARD ($SEEDS) finished"
