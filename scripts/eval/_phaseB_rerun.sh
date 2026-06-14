#!/usr/bin/env bash
# Phase B targeted rerun — fires AFTER the mobileworld-only pass completes.
# Idle-waits until every mobileworld covered id has both systems' rows AND the
# mobileworld supervisor (_phaseB_run.sh) has exited, then reruns the hand-picked
# (task, system) pairs recorded in rerun_queue.tsv (split into per-(bench,system)
# ids-files _rerun_<system>_ids.txt). Clean run: per-system device reset +
# kill_all_apps after each task are baked into run_benchmark_test.py.
set -u
cd /home/yjs/RelayAgent
RC=traj_logs/reclassify/final
PB=traj_logs/phaseB

mw_remaining () {
  python3 - "$RC/mobileworld_covered_ids.txt" "$PB/mobileworld/results.jsonl" <<'PY'
import sys, json, collections, pathlib
cov = [l.strip() for l in open(sys.argv[1]) if l.strip()]
done = set(); p = pathlib.Path(sys.argv[2])
if p.exists():
    c = collections.Counter()
    for l in p.open():
        try: c[json.loads(l)["id"]] += 1
        except Exception: pass
    done = {i for i, n in c.items() if n >= 2}
print(len([i for i in cov if i not in done]))
PY
}

echo "===== RERUN WAITER start $(date) ====="
while :; do
  rem=$(mw_remaining)
  sup=$(pgrep -f "_phaseB_run.sh" | head -1)
  [ "$rem" -eq 0 ] && [ -z "$sup" ] && break
  echo "  waiting: mobileworld remaining=$rem  supervisor=${sup:-gone}  ($(date +%H:%M:%S))"
  sleep 120
done
echo "===== mobileworld complete — starting targeted reruns $(date) ====="

run_group () {  # $1=bench  $2=system(relay|mw)
  local b="$1" s="$2" ids="$PB/$1/_rerun_$2_ids.txt"
  [ -s "$ids" ] || { echo "  (no ids for $b/$s, skip)"; return 0; }
  echo "===== RERUN $b  systems=$s  n=$(grep -c . "$ids")  ($(date +%H:%M:%S)) ====="
  # --no-judge: skip the LLM leg-judge (gateway stalls it for many minutes); the
  # final screen is still captured, judged later by hand via scripts/eval/manual_judge.py
  uv run python scripts/run_benchmark_test.py --benchmark "$b" \
    --ids-file "$ids" --systems "$s" --out-dir "$PB/$b" --no-judge || true
}

# relaybench mw (3) + relay (9) already finished cleanly — screenshots saved,
# manual-judge from those; only androiddaily remains.
# run_group relaybench   mw
# run_group relaybench   relay
run_group androiddaily mw
run_group androiddaily relay
echo "===== TARGETED RERUN DONE $(date) ====="
