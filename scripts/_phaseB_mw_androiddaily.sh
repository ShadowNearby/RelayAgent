#!/usr/bin/env bash
# AndroidDaily 未测任务 MW 补测 supervisor（mw-only）— 自愈 + 断点续跑。
# 与 _phaseB_run.sh 同构，区别：done 判定 = results.jsonl 里该 id 有 system=="mw" 行
# （A/B 版要求每 id ≥2 行，mw-only 不适用）。
# 用法：BATCH=easy scripts/_phaseB_mw_androiddaily.sh
#   BATCH ∈ easy / easy_seeded / medium / medium_seeded，
#   对应 traj_logs/phaseB/androiddaily_${BATCH}_ids.txt。
set -u
cd /home/yjs/RelayAgent
BATCH="${BATCH:?set BATCH=easy|easy_seeded|medium|medium_seeded}"
OUT=traj_logs/phaseB/androiddaily
IDS="traj_logs/phaseB/androiddaily_${BATCH}_ids.txt"
[ -f "$IDS" ] || { echo "missing $IDS"; exit 1; }
MAX_ITERS=20
TASK_TIMEOUT="${TASK_TIMEOUT:-900}"

remaining () {  # -> writes $OUT/_remaining_${BATCH}.txt, echoes count
  python3 - "$IDS" "$OUT/results.jsonl" "$OUT/_remaining_${BATCH}.txt" <<'PY'
import sys, json, pathlib
ids_f, res_f, out_f = sys.argv[1:4]
want = [l.strip() for l in open(ids_f)
        if l.strip() and not l.lstrip().startswith("#")]
done = set()
p = pathlib.Path(res_f)
if p.exists():
    for l in p.open():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("system") == "mw":
            done.add(r.get("id"))
rem = [i for i in want if i not in done]
open(out_f, "w").write("\n".join(rem) + ("\n" if rem else ""))
print(len(rem))
PY
}

kill_orphan_server () {
  local pids; pids=$(lsof -ti tcp:6800 2>/dev/null || true)
  if [ -n "$pids" ]; then echo "  [supervisor] killing orphan MW server: $pids"; kill $pids 2>/dev/null || true; sleep 2; fi
}

for i in $(seq 1 "$MAX_ITERS"); do
  n=$(remaining)
  echo "########## ITER $i  batch=$BATCH remaining=$n  ($(date '+%H:%M:%S')) ##########"
  [ "$n" -eq 0 ] && { echo "===== BATCH $BATCH DONE ====="; break; }
  kill_orphan_server
  uv run python scripts/run_benchmark_test.py --benchmark androiddaily \
    --ids-file "$OUT/_remaining_${BATCH}.txt" --systems mw \
    --out-dir "$OUT" --task-timeout "$TASK_TIMEOUT" || true
  sleep 3
done
