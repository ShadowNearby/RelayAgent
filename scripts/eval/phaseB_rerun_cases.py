#!/usr/bin/env python3
"""Phase B 定点 rerun：跑指定 (benchmark, system, task) 组合，只产出归一化时间 + token，
**不判成败**（成败之后用 scripts/eval/manual_judge.py 人工补）。

做什么
------
1. 读 rerun 清单（TSV，列同 traj_logs/phaseB/rerun_queue.tsv：``system\\ttask_id\\tbenchmark``，
   ``#`` 行注释；或用 ``--pair bench:system:id`` 直接给）。
2. 按 (benchmark, system) 分组，逐组调 ``run_benchmark_test.py --ids-file ... --systems <s>
   --no-judge``，append 进 ``traj_logs/phaseB/<bench>/results.jsonl``（mw 在前、relay 在后，
   mobileworld 自动带 ``--skip-mcp``；每个 task 的设备复位已 baked 进 driver）。
3. 对清单里每个 pair 取 results.jsonl 的**最后一行**，用定标常数
   （默认 ``traj_logs/phaseB/wall_norm_rounded.json``：model_time = gamma + alpha*(prompt-cached)
   + beta*completion）就地重算 ``wall_norm = elapsed_s - Σ实际LLM延迟 + Σ模型LLM时间``，
   连同 token 用量写 ``traj_logs/phaseB/rerun_report.csv``。success 列**留空**给人工判。

报表的 ``fresh`` 列：本次脚本确实跑出了新行=yes；行还是老的（rerun 没跑成/被跳过）=no——
``--no-run`` 模式下全部标 no，仅汇总现状。

用法
----
    uv run python scripts/eval/phaseB_rerun_cases.py --queue traj_logs/phaseB/rerun2.tsv
    uv run python scripts/eval/phaseB_rerun_cases.py --queue ... --no-run     # 只重算报表，不跑
    uv run python scripts/eval/phaseB_rerun_cases.py --pair androiddaily:relay:AD-002 \\
        --pair androiddaily:mw:AD-002
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PHASEB = REPO / "traj_logs" / "phaseB"
BENCH_ORDER = ["relaybench", "androiddaily", "mobileworld"]
SYS_ORDER = ["mw", "relay"]  # 与 _phaseB_rerun.sh 一致：每个 bench 先 mw 后 relay


# ---- 清单 ----------------------------------------------------------------
def load_pairs(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """返回 (benchmark, system, task_id) 列表，保序去重。"""
    pairs: list[tuple[str, str, str]] = []
    if args.queue:
        for ln in args.queue.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            cols = ln.split("\t")
            if len(cols) != 3:
                raise SystemExit(f"bad queue line (want system\\tid\\tbench): {ln!r}")
            system, task_id, bench = (c.strip() for c in cols)
            pairs.append((bench, system, task_id))
    for spec in args.pair or []:
        try:
            bench, system, task_id = spec.split(":", 2)
        except ValueError:
            raise SystemExit(f"bad --pair (want bench:system:id): {spec!r}")
        pairs.append((bench, system, task_id))
    seen: set[tuple[str, str, str]] = set()
    out = [p for p in pairs if not (p in seen or seen.add(p))]
    if not out:
        raise SystemExit("empty rerun list — give --queue and/or --pair")
    for bench, system, _ in out:
        if bench not in BENCH_ORDER or system not in SYS_ORDER:
            raise SystemExit(f"unknown bench/system in pair {(bench, system)}")
    return out


# ---- 归一化（与 phaseB_summary 同款：定标常数 gamma+alpha+beta） ----------
def load_norm_constants(fit_path: Path) -> tuple[float, float, float]:
    fit = json.loads(fit_path.read_text())
    return (float(fit.get("gamma_s_per_call") or 0.0),
            float(fit["alpha_s_per_prefill_tok"]),
            float(fit["beta_s_per_decode_tok"]))


def _call_latency(c: dict[str, Any]) -> float | None:
    v = c.get("elapsed_s")  # mw probe
    if v is None:
        v = c.get("latency_s")  # relay token_usage
    return None if v is None else float(v)


def usable_calls(row: dict[str, Any]) -> list[tuple[float, int, int, int]]:
    out: list[tuple[float, int, int, int]] = []
    for c in row.get("llm_calls") or []:
        if c.get("error"):
            continue
        t = _call_latency(c)
        p, comp = c.get("prompt_tokens"), c.get("completion_tokens")
        if t is None or p is None or comp is None:
            continue
        out.append((t, int(p), int(comp), int(c.get("cached_tokens") or 0)))
    return out


def normalize_row(row: dict[str, Any], gamma: float, alpha: float, beta: float) -> dict[str, float]:
    calls = usable_calls(row)
    llm_act = sum(t for t, _, _, _ in calls)
    llm_norm = sum(gamma + alpha * (p - cached) + beta * comp for _, p, comp, cached in calls)
    raw = float(row.get("elapsed_s") or 0.0)
    wall_norm = raw - llm_act + llm_norm
    if wall_norm < 0:  # 实际 LLM 延迟超 elapsed（并行等），同 normalize_wall_clock：clamp 到模型时间
        print(f"  [warn] {row.get('id')}/{row.get('system')}: negative wall_norm "
              f"({wall_norm:.1f}) — clamping to llm_norm")
        wall_norm = llm_norm
    return {"elapsed_s_raw": round(raw, 3), "llm_time_actual_s": round(llm_act, 3),
            "llm_time_norm_s": round(llm_norm, 3), "elapsed_s_norm": round(wall_norm, 3),
            "n_llm_calls": len(calls)}


# ---- results.jsonl 读取 ----------------------------------------------------
def read_rows(results: Path) -> list[dict[str, Any]]:
    if not results.exists():
        return []
    rows: list[dict[str, Any]] = []
    for ln in results.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            print(f"  [warn] skip malformed line in {results}")
    return rows


def last_row(rows: list[dict[str, Any]], task_id: str, system: str) -> tuple[dict[str, Any] | None, int]:
    """(最后一条匹配行, 其行号)；没有则 (None, -1)。"""
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if r.get("id") == task_id and r.get("system") == system:
            return r, i
    return None, -1


# ---- 跑 rerun ---------------------------------------------------------------
def kill_orphan_mw_server() -> None:
    """best-effort 清掉上次 driver 暴毙留下的 MW server（占 6800 端口）。"""
    try:
        out = subprocess.run(["lsof", "-ti", "tcp:6800"], capture_output=True, text=True)
        pids = out.stdout.split()
        if pids:
            print(f"  [run] killing orphan MW server pid(s): {' '.join(pids)}")
            subprocess.run(["kill", *pids])
    except FileNotFoundError:
        pass


def run_group(bench: str, system: str, ids: list[str], out_dir: Path,
              task_timeout: float) -> None:
    ids_file = out_dir / f"_rerun_cases_{system}_ids.txt"
    out_dir.mkdir(parents=True, exist_ok=True)
    ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
    cmd = [sys.executable, str(REPO / "scripts" / "run_benchmark_test.py"),
           "--benchmark", bench, "--ids-file", str(ids_file), "--systems", system,
           "--out-dir", str(out_dir), "--no-judge", "--task-timeout", str(task_timeout)]
    if bench == "mobileworld":
        cmd.append("--skip-mcp")
    if system == "mw":
        kill_orphan_mw_server()
    print(f"===== RERUN {bench}/{system}  n={len(ids)} =====")
    print("  $ " + " ".join(cmd))
    # 单组失败不挡后面的组；缺的行在报表里以 fresh=no 暴露
    subprocess.run(cmd, cwd=REPO)


# ---- main -------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", type=Path, default=None,
                    help="rerun 清单 TSV（system\\ttask_id\\tbenchmark，# 注释）")
    ap.add_argument("--pair", action="append", default=None, metavar="BENCH:SYSTEM:ID",
                    help="附加单条，如 androiddaily:relay:AD-002（可重复）")
    ap.add_argument("--no-run", action="store_true",
                    help="不跑任务，只对清单里的 pair 重算报表")
    ap.add_argument("--phaseb-dir", type=Path, default=PHASEB)
    ap.add_argument("--norm-fit", type=Path, default=None,
                    help=f"归一化常数 JSON（默认 <phaseb-dir>/wall_norm_rounded.json）")
    ap.add_argument("--report-out", type=Path, default=None,
                    help="报表 CSV 路径（默认 <phaseb-dir>/rerun_report.csv）")
    ap.add_argument("--data-csv", type=Path, default=None,
                    help="把 time/norm/token 原地 fold 进这张汇总表（每任务一行，"
                         "mw_* / ra_* 列）；默认 <phaseb-dir>/data.csv。success 列不动。"
                         "传 'none' 关掉")
    ap.add_argument("--task-timeout", type=float, default=900.0)
    args = ap.parse_args(argv)

    pairs = load_pairs(args)
    fit_path = args.norm_fit or args.phaseb_dir / "wall_norm_rounded.json"
    gamma, alpha, beta = load_norm_constants(fit_path)
    print(f"norm constants ({fit_path.name}): gamma={gamma}s/call  "
          f"alpha={alpha} s/prefill_tok  beta={beta} s/decode_tok")

    # 跑之前记每个 results.jsonl 的行数，跑完用行号判 fresh。
    pre_lines: dict[str, int] = {}
    for bench in {b for b, _, _ in pairs}:
        pre_lines[bench] = len(read_rows(args.phaseb_dir / bench / "results.jsonl"))

    if not args.no_run:
        groups: dict[tuple[str, str], list[str]] = {}
        for bench, system, task_id in pairs:
            groups.setdefault((bench, system), []).append(task_id)
        for bench in BENCH_ORDER:
            for system in SYS_ORDER:
                ids = groups.get((bench, system))
                if ids:
                    run_group(bench, system, ids, args.phaseb_dir / bench, args.task_timeout)

    # ---- 报表：每 pair 取最后一行，归一化时间 + token，success 留空 ----
    cols = ["benchmark", "id", "system", "fresh", "success",
            "elapsed_s_raw", "llm_time_actual_s", "llm_time_norm_s", "elapsed_s_norm",
            "n_llm_calls", "prompt_tokens", "completion_tokens", "total_tokens",
            "steps", "terminal_action", "returncode", "timed_out", "flow_root"]
    report: list[dict[str, Any]] = []
    rows_by_bench = {b: read_rows(args.phaseb_dir / b / "results.jsonl")
                     for b in {bb for bb, _, _ in pairs}}
    for bench, system, task_id in pairs:
        row, idx = last_row(rows_by_bench[bench], task_id, system)
        if row is None:
            print(f"  [warn] no row for {bench}/{system}/{task_id} — skipping in report")
            report.append({"benchmark": bench, "id": task_id, "system": system,
                           "fresh": "no", "success": ""})
            continue
        rec: dict[str, Any] = {
            "benchmark": bench, "id": task_id, "system": system,
            "fresh": "yes" if idx >= pre_lines[bench] else "no",
            "success": "",  # 人工判：scripts/eval/manual_judge.py
            **normalize_row(row, gamma, alpha, beta),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "total_tokens": row.get("total_tokens"),
            "steps": row.get("steps"), "terminal_action": row.get("terminal_action"),
            "returncode": row.get("returncode"), "timed_out": row.get("timed_out"),
            "flow_root": row.get("flow_root") or "",
        }
        report.append(rec)

    out_csv = args.report_out or args.phaseb_dir / "rerun_report.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for rec in report:
            w.writerow({c: rec.get(c, "") for c in cols})

    print(f"\n{'bench':12s} {'id':26s} {'sys':5s} {'fresh':5s} {'raw_s':>8s} "
          f"{'norm_s':>8s} {'tokens':>8s}")
    for rec in report:
        def _s(k: str) -> str:
            v = rec.get(k)
            return "" if v is None else str(v)
        print(f"{rec['benchmark']:12s} {str(rec['id'])[:26]:26s} {rec['system']:5s} "
              f"{rec['fresh']:5s} {_s('elapsed_s_raw'):>8} "
              f"{_s('elapsed_s_norm'):>8} {_s('total_tokens'):>8}")
    print(f"\nwrote {out_csv}  ({len(report)} rows; success 列留空，"
          f"用 scripts/eval/manual_judge.py 人工判后再汇总)")

    # ---- 可选：原地更新汇总表 data.csv 的 time/norm/token 列（success 列不动）----
    data_csv = args.phaseb_dir / "data.csv" if args.data_csv is None else args.data_csv
    if str(data_csv).lower() != "none":
        update_data_csv(Path(data_csv), report)
    return 0


# 系统 -> (time列, norm列, token列)；data.csv 用 mw_* / ra_*（ra==relay）。
_CSV_COLS = {"mw": ("mw_time_s", "mw_norm_s", "mw_tokens"),
             "relay": ("ra_time_s", "ra_norm_s", "ra_tokens")}


def update_data_csv(path: Path, report: list[dict[str, Any]]) -> None:
    """把 report 里跑出新数据的行，按 (benchmark,id) 定位到 data.csv 同一行，
    原地改写该 system 的 time/norm/token 三列。success / fallback 列一律不动。"""
    if not path.exists():
        print(f"  [warn] data.csv 不存在，跳过原地更新：{path}")
        return
    with path.open(encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        fieldnames = rdr.fieldnames or []
        rows = list(rdr)
    index = {(r.get("benchmark"), r.get("id")): r for r in rows}

    updated, missing = 0, []
    for rec in report:
        if rec.get("fresh") != "yes":
            continue  # 只 fold 本次真跑出新数据的行
        key = (rec["benchmark"], rec["id"])
        target = index.get(key)
        if target is None:
            missing.append(f"{key[0]}/{key[1]}")
            continue
        cols = _CSV_COLS.get(rec["system"])
        if cols is None:
            continue
        time_c, norm_c, tok_c = cols
        raw, norm, tok = rec.get("elapsed_s_raw"), rec.get("elapsed_s_norm"), rec.get("total_tokens")
        if time_c in target:
            target[time_c] = "" if raw is None else round(float(raw), 1)
        if norm_c in target:
            target[norm_c] = "" if norm is None else round(float(norm), 1)
        if tok_c in target:
            target[tok_c] = "" if tok is None else tok
        updated += 1
        print(f"  [data.csv] {rec['benchmark']}/{rec['id']}/{rec['system']}: "
              f"{time_c}={target.get(time_c)} {norm_c}={target.get(norm_c)} {tok_c}={target.get(tok_c)}")

    if missing:
        print(f"  [warn] data.csv 无对应行（未更新）：{', '.join(missing)}")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  原地更新 {path}（{updated} 个 system 单元；success 列未动）")


if __name__ == "__main__":
    raise SystemExit(main())
