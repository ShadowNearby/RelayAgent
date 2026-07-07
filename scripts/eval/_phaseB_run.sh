#!/usr/bin/env bash
# Phase B real-device A/B over the FINAL covered sets — SELF-HEALING + RESUME-AWARE.
# Each iteration: kill any orphan MW server, then run every benchmark on its
# REMAINING ids (covered minus those already fully A/B'd in results.jsonl). Repeats
# until nothing remains or MAX_ITERS hit, so a single driver death doesn't stall the
# run (bash supervisor survives and re-runs). Order: relaybench -> androiddaily ->
# mobileworld (Gemini-account-sensitive last).
set -u
cd "$(dirname "$0")/../.."   # repo root
RC=traj_logs/reclassify/final
MAX_ITERS=40

remaining () {  # $1=bench -> writes <out>/_remaining.txt, echoes count
  local b="$1" out="traj_logs/phaseB/$1"
  mkdir -p "$out"
  python3 - "$RC/${b}_covered_ids.txt" "$out/results.jsonl" "$out/_remaining.txt" <<'PY'
import sys, json, collections, pathlib
cov_f, res_f, out_f = sys.argv[1:4]
cov = [l.strip() for l in open(cov_f) if l.strip()]
done = set()
p = pathlib.Path(res_f)
if p.exists():
    cnt = collections.Counter()
    for l in p.open():
        try: r = json.loads(l)
        except Exception: continue
        cnt[r["id"]] += 1
    done = {i for i, c in cnt.items() if c >= 2}
rem = [i for i in cov if i not in done]
open(out_f, "w").write("\n".join(rem) + ("\n" if rem else ""))
print(len(rem))
PY
}

kill_orphan_server () {  # free port 6800 left by a killed driver (by PID, safe)
  local pids; pids=$(lsof -ti tcp:6800 2>/dev/null || true)
  if [ -n "$pids" ]; then echo "  [supervisor] killing orphan MW server: $pids"; kill $pids 2>/dev/null || true; sleep 2; fi
}

run_bench () {  # $1=bench  $2..=extra driver args
  local b="$1"; shift
  local out="traj_logs/phaseB/$b" n
  n=$(remaining "$b")
  echo "===== $b  remaining=$n ====="
  [ "$n" -eq 0 ] && { echo "  (complete)"; return 0; }
  uv run python scripts/run_benchmark_test.py --benchmark "$b" \
    --ids-file "$out/_remaining.txt" --systems mw,relay --out-dir "$out" "$@" || true
}

total_remaining () {
  local s=0 b
  for b in mobileworld; do s=$((s + $(remaining "$b"))); done
  echo "$s"
}

for i in $(seq 1 "$MAX_ITERS"); do
  echo "########## SUPERVISOR ITER $i  ($(date '+%H:%M:%S')) ##########"
  kill_orphan_server
  run_bench mobileworld --skip-mcp
  # androiddaily / relaybench intentionally paused — only mobileworld this pass.
  # run_bench androiddaily
  # run_bench relaybench
  rem=$(total_remaining)
  echo "########## after iter $i: total remaining=$rem ##########"
  [ "$rem" -eq 0 ] && { echo "===== PHASE B DONE ====="; break; }
  sleep 3
done
