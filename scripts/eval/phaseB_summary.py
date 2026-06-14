#!/usr/bin/env python3
"""Phase B 汇总：把三个 benchmark 的 A/B 结果拍平成一张「每任务一行」的表。

每行给出 task 具体内容 + mw / relay(ra) 两侧的 成功 / 时间 / 归一化时间 / token：

    benchmark | id | task | category | apps |
    mw_success  mw_time_s  mw_norm_s  mw_tokens |
    ra_success  ra_time_s  ra_norm_s  ra_tokens  ra_mw_fallback  ra_mw_legs

ra_mw_fallback = RA 这条任务是否有 leg 落到 MobileWorld 兜底（即靠 baseline 完成），
ra_mw_legs = "MW leg 数/总 leg 数"。判定依据：results.jsonl 行里的 flow_root 下，
每个 leg 目录的 summary.json 带 `via: "mobileworld"` 即为 MW 兜底 leg
（flow_runner._run_mobileworld_step 写入）。flow_root 缺失/已删则两列留空。

数据来源（均为本地，不联网）：
  - task 内容：各 benchmark 的本地任务表
      relaybench   -> benchmark/relaybench_tasks.yaml      (id -> instruction)
      androiddaily -> benchmark/androiddaily_task_info.csv (AD-{行号:03d} -> 任务)
      mobileworld  -> benchmark/mobileworld_benchmark_task_info.csv (Task Name -> Goal)
  - 运行结果：traj_logs/phaseB/<bench>/results.jsonl（最新；每 (id,system) 取最后一行）
      success = verdict.status == "success"
      time    = elapsed_s（原始子进程墙钟）
      tokens  = total_tokens
  - 归一化时间：用 normalize_wall_clock 的模型时间就地重算（不读可能过期的
      results_normalized.jsonl）：wall_norm = elapsed_s - Σ实际LLM延迟 + Σ模型LLM时间，
      模型时间 = gamma + alpha*(prompt-cached) + beta*completion，常数取
      traj_logs/phaseB/wall_norm_rounded.json（gamma/alpha/beta）。

用法：
    uv run python scripts/eval/phaseB_summary.py
    uv run python scripts/eval/phaseB_summary.py --out traj_logs/phaseB/summary.csv \
        --norm-fit traj_logs/phaseB/wall_norm_rounded.json
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PHASEB = REPO / "traj_logs" / "phaseB"
BENCH_DIR = REPO / "benchmark"

RELAYBENCH_YAML = BENCH_DIR / "relaybench_tasks.yaml"
ANDROIDDAILY_CSV = BENCH_DIR / "androiddaily_task_info.csv"
MOBILEWORLD_CSV = BENCH_DIR / "mobileworld_benchmark_task_info.csv"

BENCHMARKS = ["relaybench", "androiddaily", "mobileworld"]


# ---- task 内容映射：id -> instruction（与 run_benchmark_test 的 loader 对齐）----
def _relaybench_tasks() -> dict[str, str]:
    import yaml
    doc = yaml.safe_load(RELAYBENCH_YAML.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for t in doc.get("tasks") or []:
        tid = t.get("id")
        if tid:
            out[tid] = (t.get("instruction") or "").strip()
    return out


def _androiddaily_tasks() -> dict[str, str]:
    # id = AD-{i:03d}，i 从 1 计数所有数据行；空「任务」行仍占一个序号（与 loader 一致）。
    rows = list(csv.DictReader(ANDROIDDAILY_CSV.open(encoding="utf-8")))
    out: dict[str, str] = {}
    for i, r in enumerate(rows, 1):
        instr = (r.get("任务") or "").strip()
        if not instr:
            continue
        out[f"AD-{i:03d}"] = instr
    return out


def _mobileworld_tasks() -> dict[str, str]:
    rows = list(csv.DictReader(MOBILEWORLD_CSV.open(encoding="utf-8")))
    out: dict[str, str] = {}
    for r in rows:
        name = (r.get("Task Name") or "").strip()
        if name:
            out[name] = (r.get("Goal") or "").strip()
    return out


def load_task_text(bench: str) -> dict[str, str]:
    return {
        "relaybench": _relaybench_tasks,
        "androiddaily": _androiddaily_tasks,
        "mobileworld": _mobileworld_tasks,
    }[bench]()


# ---- 归一化时间：就地重算（搬自 normalize_wall_clock，避免读过期的 normalized 文件）----
def _call_latency(c: dict[str, Any]) -> float | None:
    v = c.get("elapsed_s")
    if v is None:
        v = c.get("latency_s")
    return None if v is None else float(v)


def _usable_calls(row: dict[str, Any]) -> list[tuple[float, int, int, int]]:
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


def _load_norm_constants(fit_path: Path) -> tuple[float, float, float]:
    fit = json.loads(fit_path.read_text())
    gamma = float(fit.get("gamma_s_per_call") or 0.0)
    alpha = float(fit["alpha_s_per_prefill_tok"])
    beta = float(fit["beta_s_per_decode_tok"])
    return gamma, alpha, beta


def norm_seconds(row: dict[str, Any], const: tuple[float, float, float]) -> float | None:
    raw = row.get("elapsed_s")
    if raw is None:
        return None
    raw = float(raw)
    gamma, alpha, beta = const
    cs = _usable_calls(row)
    llm_act = sum(t for t, _, _, _ in cs)
    llm_norm = sum(gamma + alpha * (p - cached) + beta * comp for _, p, comp, cached in cs)
    wall_norm = raw - llm_act + llm_norm
    if wall_norm < 0:  # 与 normalize 一致：实际 LLM 时间超过墙钟（并行）→ 退回模型时间
        wall_norm = llm_norm
    return round(wall_norm, 1)


# ---- 读 results.jsonl：每 (id, system) 取最后一行 ----
def load_results(bench: str) -> dict[str, dict[str, dict[str, Any]]]:
    """returns id -> {system -> row}（system: 'mw' / 'relay'）。"""
    path = PHASEB / bench / "results.jsonl"
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    if not path.exists():
        print(f"[warn] 缺 {path}")
        return by_task
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid, sysn = r.get("id"), r.get("system")
        if tid is None or sysn is None:
            continue
        by_task.setdefault(tid, {})[sysn] = r  # 后写覆盖 → 最后一行胜出
    return by_task


def _success(row: dict[str, Any] | None) -> int | str:
    if row is None:
        return ""
    v = row.get("verdict")
    if not isinstance(v, dict) or v.get("status") is None:
        return ""
    return int(v.get("status") == "success")


def _time(row: dict[str, Any] | None) -> str:
    if row is None or row.get("elapsed_s") is None:
        return ""
    return f"{float(row['elapsed_s']):.1f}"


def _norm(row: dict[str, Any] | None, const) -> str:
    if row is None:
        return ""
    v = norm_seconds(row, const)
    return "" if v is None else f"{v:.1f}"


def _tokens(row: dict[str, Any] | None) -> int | str:
    if row is None or row.get("total_tokens") is None:
        return ""
    return int(row["total_tokens"])


def _ra_mw_fallback(row: dict[str, Any] | None) -> tuple[int | str, str]:
    """RA 是否靠 MobileWorld 兜底 leg 完成：(ra_mw_fallback, "mw/total")。

    扫 flow_root 下每个 leg 目录的 summary.json，`via == "mobileworld"` 即
    MW 兜底 leg。flow_root 缺失/已被清理时返回 ("", "")（区别于确定的 0）。"""
    if row is None or not row.get("flow_root"):
        return "", ""
    root = Path(row["flow_root"])
    if not root.is_dir():
        return "", ""
    total = n_mw = 0
    for leg in sorted(p for p in root.glob("[0-9]*_*") if p.is_dir()):
        total += 1
        try:
            summary = json.loads((leg / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("via") == "mobileworld":
            n_mw += 1
    if total == 0:
        return "", ""
    return int(n_mw > 0), f"{n_mw}/{total}"


HEADER = [
    "benchmark", "id", "task", "category", "apps",
    "mw_success", "mw_time_s", "mw_norm_s", "mw_tokens",
    "ra_success", "ra_time_s", "ra_norm_s", "ra_tokens",
    "ra_mw_fallback", "ra_mw_legs",
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=PHASEB / "summary.csv")
    ap.add_argument("--norm-fit", type=Path, default=PHASEB / "wall_norm_rounded.json",
                    help="归一化常数文件（gamma/alpha/beta）")
    ap.add_argument("--benchmarks", nargs="+", default=BENCHMARKS, choices=BENCHMARKS)
    args = ap.parse_args()

    const = _load_norm_constants(args.norm_fit)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    counts: dict[str, dict[str, int]] = {}
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for bench in args.benchmarks:
            text = load_task_text(bench)
            results = load_results(bench)
            counts[bench] = {"tasks": 0, "both": 0, "mw_ok": 0, "ra_ok": 0,
                             "ra_fb": 0, "ra_fb_ok": 0}
            for tid in sorted(results):
                sysrows = results[tid]
                mw = sysrows.get("mw")
                ra = sysrows.get("relay")
                meta = mw or ra or {}
                apps = meta.get("apps") or ([meta.get("app")] if meta.get("app") else [])
                ra_mw_flag, ra_mw_legs = _ra_mw_fallback(ra)
                w.writerow([
                    bench, tid, text.get(tid, ""),
                    meta.get("category") or "", "|".join(a for a in apps if a),
                    _success(mw), _time(mw), _norm(mw, const), _tokens(mw),
                    _success(ra), _time(ra), _norm(ra, const), _tokens(ra),
                    ra_mw_flag, ra_mw_legs,
                ])
                n_rows += 1
                counts[bench]["tasks"] += 1
                if mw is not None and ra is not None:
                    counts[bench]["both"] += 1
                if _success(mw) == 1:
                    counts[bench]["mw_ok"] += 1
                if _success(ra) == 1:
                    counts[bench]["ra_ok"] += 1
                if ra_mw_flag == 1:
                    counts[bench]["ra_fb"] += 1
                    if _success(ra) == 1:
                        counts[bench]["ra_fb_ok"] += 1

    print(f"归一化常数 ({args.norm_fit.name}): "
          f"gamma={const[0]:.4g} alpha={const[1]:.4g} beta={const[2]:.4g}")
    print(f"{'benchmark':14s} {'tasks':>6s} {'both':>6s} {'mw_ok':>6s} {'ra_ok':>6s} "
          f"{'ra_fb':>6s} {'fb_ok':>6s}")
    for bench in args.benchmarks:
        c = counts.get(bench, {})
        print(f"{bench:14s} {c.get('tasks',0):6d} {c.get('both',0):6d} "
              f"{c.get('mw_ok',0):6d} {c.get('ra_ok',0):6d} "
              f"{c.get('ra_fb',0):6d} {c.get('ra_fb_ok',0):6d}")
    print("（ra_fb = RA 有 MobileWorld 兜底 leg 的任务数；fb_ok = 其中成功的）")
    print(f"\n{n_rows} 行 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
