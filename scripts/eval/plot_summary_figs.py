#!/usr/bin/env python3
"""从 phaseB_summary.py 产出的 summary.csv 画 fig1 / fig6 / fig7。

输入：traj_logs/phaseB/summary.csv（`scripts/eval/phaseB_summary.py` 的产物）。
输出：docs/eval_figs/{fig1_coverage,fig6_paired_tokens,fig7_paired_time}.{png,pdf}

  fig1_coverage      — 各 benchmark 的 covered/fallback 分层堆叠条（plan-only）。
  fig6_paired_tokens — covered both-success 交集上，逐任务 RA vs baseline 的 token。
  fig7_paired_time   — 同一交集上，逐任务 RA vs baseline 的（归一化）墙钟。

数据口径
--------
- fig6/fig7 只用 summary.csv：取 mw 和 ra 两侧都判成功（both-success）的任务，逐任务
  把 RA(蓝)与 baseline(橙)纵向对齐画两点 + 连线，x 仅为按 baseline/RA 比值排序的 rank。
  AndroidDaily 里 RA 无 manifest 的跨 App 任务（按 APP_NORM 表）剔除，保留 covered-app 层
  —— 与 plot_eval_figs.py 同口径。注意 summary.csv 不带 relay_legs，故无法再细分运行期是否
  真跑了垂类路由（phaseB 本就只跑 covered-id 集，差异可忽略）。
- fig1 的分层计数（covered/foundation_fallback/mixed/mw）来自 **plan-only 分类**
  （docs/evaluation.zh.md §11），不在 summary.csv 里，作为常量 TIERS 内嵌（与
  plot_eval_figs.py 保持一致）。

时间口径：fig7 默认用 **归一化时间**（summary.csv 的 *_norm_s，本项目的公平指标）。
`--raw-time` 改用原始墙钟（*_time_s）。

用法：
    uv run python scripts/eval/plot_summary_figs.py
    uv run python scripts/eval/plot_summary_figs.py --csv traj_logs/phaseB/summary.csv --raw-time
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "docs" / "eval_figs"
DEFAULT_CSV = REPO / "traj_logs" / "phaseB" / "summary.csv"

# benchmark 内部名 -> 展示名
BENCH_LABEL = {"relaybench": "RelayBench", "androiddaily": "AndroidDaily",
               "mobileworld": "MobileWorld"}
ALL_BENCHES = ["relaybench", "androiddaily", "mobileworld"]

# ── 配色（与 plot_eval_figs.py 一致，跨图统一）──
RELAY = "#0072B2"      # 蓝 — RelayAgent
BASELINE = "#D55E00"   # 橙 — MobileWorld general_e2e
TIER_COLORS = {"covered": "#1B7837", "foundation_fallback": "#A6DBA0",
               "mixed": "#9970AB", "mw": "#C2A5CF"}
TIER_LABEL = {"covered": "covered (specialized route)",
              "foundation_fallback": "foundation fallback",
              "mixed": "mixed (MW + specialized)", "mw": "MW fallback (= baseline)"}

# fig1：plan-only leg-kind 分类（docs/evaluation.zh.md §11, 2026-06-10）。三个 benchmark
# 完整（即便 A/B 只跑了部分）。来源同 plot_eval_figs.py，非 summary.csv。
TIERS = {
    "RelayBench":   {"covered": 27, "foundation_fallback": 3,  "mixed": 0, "mw": 0},
    "AndroidDaily": {"covered": 71, "foundation_fallback": 19, "mixed": 2, "mw": 143},
    "MobileWorld":  {"covered": 61, "foundation_fallback": 10, "mixed": 0, "mw": 90},
}

# 名义 App -> (展示名, RA 是否有 manifest)；用于 fig6/fig7 的 covered-app 过滤。
APP_NORM = {
    "高德地图": ("Amap", True), "携程": ("Ctrip", True), "携程旅行": ("Ctrip", True),
    "淘宝": ("Taobao", True), "小红书": ("XHS", True),
    "铁路12306": ("12306", False), "12306": ("12306", False), "飞猪": ("Fliggy", False),
    "同程旅行": ("Tongcheng", False), "去哪儿旅行": ("Qunar", False), "去哪儿": ("Qunar", False),
    "京东": ("JD", False), "滴滴出行": ("DiDi", False), "美团": ("Meituan", False),
    "com.aliyun.tongyi": ("Tongyi", True), "com.autonavi.minimap": ("Amap", True),
    "ctrip.android.view": ("Ctrip", True), "com.google.android.apps.bard": ("Gemini", True),
    "com.tencent.mm": ("WeChat", True), "com.xingin.xhs": ("XHS", True),
    "cn.wps.moffice_eng": ("WPS", True), "com.booking": ("Booking", True),
    "com.reddit.frontpage": ("Reddit", True), "com.microsoft.copilot": ("Copilot", True),
}

mpl.rcParams.update({
    "figure.dpi": 130, "savefig.dpi": 200, "font.size": 11,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.6,
})


# ── summary.csv 读取 ──
def _f(s: str) -> float | None:
    s = (s or "").strip()
    return float(s) if s else None


def _i(s: str) -> int | None:
    s = (s or "").strip()
    return int(s) if s else None


def load_paired(csv_path: Path, *, use_raw_time: bool) -> dict[str, list[dict]]:
    """benchmark -> 逐任务 paired 记录（两侧都有行的任务）。

    每条记录：task, ra_ok, base_ok, ra_t/base_t（时间）, ra_k/base_k（token）,
    covered_app（名义 App 有 RA manifest）。时间列按 use_raw_time 选 *_time_s 或 *_norm_s。
    """
    tcol_mw = "mw_time_s" if use_raw_time else "mw_norm_s"
    tcol_ra = "ra_time_s" if use_raw_time else "ra_norm_s"
    out: dict[str, list[dict]] = {b: [] for b in ALL_BENCHES}
    with csv_path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            bench = r["benchmark"]
            if bench not in out:
                continue
            mw_ok, ra_ok = _i(r["mw_success"]), _i(r["ra_success"])
            # 两侧都要有结果行（success 列非空表示该系统跑了且有 verdict）
            if mw_ok is None or ra_ok is None:
                continue
            # covered-app 过滤只对 AndroidDaily 有意义（剔除路由到非 manifest App 的跨 App
            # 任务）。RelayBench 的 App 全是 manifest App；MobileWorld 的 App（Calendar/
            # Messages/…）不在该表里，但 RA 照样跑了，故不按此表过滤。
            first_app = (r["apps"].split("|")[0] if r["apps"] else "")
            covered_app = (APP_NORM.get(first_app, ("", False))[1]
                           if bench == "androiddaily" else True)
            out[bench].append(dict(
                task=r["id"], ra_ok=bool(ra_ok), base_ok=bool(mw_ok),
                covered_app=covered_app,
                ra_t=_f(r[tcol_ra]), base_t=_f(r[tcol_mw]),
                ra_k=_i(r["ra_tokens"]), base_k=_i(r["mw_tokens"]),
            ))
    return out


def _save(fig, stem: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(OUT_DIR / f"{stem}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {OUT_DIR / stem}.png / .pdf")


# ── fig1：coverage 分层堆叠条 ──
def fig1_coverage() -> None:
    benches = list(TIERS)
    fig, ax = plt.subplots(figsize=(8.2, 2.8))
    order = ["covered", "foundation_fallback", "mixed", "mw"]
    y = np.arange(len(benches))[::-1]
    for i, b in enumerate(benches):
        tiers = TIERS[b]
        total = sum(tiers.values()) or 1
        left = 0.0
        for t in order:
            w = 100.0 * tiers.get(t, 0) / total
            ax.barh(y[i], w, left=left, color=TIER_COLORS[t], edgecolor="white",
                    height=0.62, label=TIER_LABEL[t] if i == 0 else None)
            if w >= 7:
                ax.text(left + w / 2, y[i], f"{w:.0f}%", ha="center", va="center",
                        color="white" if t in ("covered", "mw") else "#333",
                        fontsize=9, fontweight="bold")
            left += w
        ax.text(101, y[i], f"N={total}", va="center", ha="left", fontsize=9, color="#555")
    ax.set_yticks(y, benches)
    ax.set_xlim(0, 108)
    ax.set_xlabel("share of tasks (%)")
    ax.set_title("Coverage stratification: where RelayAgent routes to a specialized agent",
                 fontsize=11, loc="left")
    ax.grid(axis="y", visible=False)
    ax.legend(ncol=2, fontsize=8.5, loc="lower center", bbox_to_anchor=(0.5, -0.55),
              frameon=False)
    _save(fig, "fig1_coverage")


# ── fig6/fig7：逐任务 paired scatter ──
def _paired_scatter(paired: dict[str, list[dict]], metric: str, ylabel: str,
                    stem: str, title: str) -> None:
    rak, bak = f"ra_{metric}", f"base_{metric}"
    benches = [b for b in ALL_BENCHES if paired.get(b)]
    fig, axes = plt.subplots(1, len(benches), figsize=(4 * len(benches), 3.7),
                             sharey=True, squeeze=False)
    axes = axes[0]
    for ax, b in zip(axes, benches):
        recs = [r for r in paired[b]
                if r["ra_ok"] and r["base_ok"] and r["covered_app"] and r[rak] and r[bak]]
        recs.sort(key=lambda r: r[bak] / r[rak], reverse=True)
        ra = np.array([r[rak] for r in recs], dtype=float)
        ba = np.array([r[bak] for r in recs], dtype=float)
        xs = np.arange(len(recs))
        for xi, rv, bv in zip(xs, ra, ba):
            ax.plot([xi, xi], [rv, bv], color="#e8a0a0" if rv > bv else "#cfcfcf",
                    lw=0.7, zorder=1)
        ax.scatter(xs, ba, s=18, zorder=3, facecolors=BASELINE, edgecolors=BASELINE,
                   linewidths=1.0, label="MW general_e2e")
        ax.scatter(xs, ra, s=18, zorder=3, facecolors=RELAY, edgecolors=RELAY,
                   linewidths=1.0, label="RelayAgent")
        ax.set_yscale("log")
        ax.set_title(f"{BENCH_LABEL[b]}  (n={len(recs)})", fontsize=10)
        ax.set_xticks([])
        ax.set_xlabel("covered both-success tasks\n← sorted by baseline/RA ratio", fontsize=8.5)
        if len(recs):
            med = float(np.median(ba / ra))
            win = float(np.mean(ba > ra) * 100)
            ax.text(0.97, 0.96, f"median {med:.1f}×\nRA cheaper {win:.0f}%",
                    transform=ax.transAxes, ha="right", va="top", fontsize=8.6,
                    color="#222",
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#ccc", alpha=0.9))
    axes[0].set_ylabel(ylabel)
    axes[0].legend(fontsize=8.5, loc="lower left", frameon=False)
    fig.suptitle(title, fontsize=12, y=1.02)
    fig.tight_layout()
    _save(fig, stem)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--raw-time", action="store_true",
                    help="fig7 用原始墙钟（默认用归一化时间）")
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"找不到 {args.csv}；先跑 scripts/eval/phaseB_summary.py")

    paired = load_paired(args.csv, use_raw_time=args.raw_time)
    print(f"rendering fig1/6/7 from {args.csv} -> {OUT_DIR}")
    for b in ALL_BENCHES:
        both = sum(r["ra_ok"] and r["base_ok"] and r["covered_app"] for r in paired[b])
        print(f"  {BENCH_LABEL[b]:14s}: {len(paired[b])} paired, {both} covered both-success")

    fig1_coverage()
    time_lbl = "wall-clock s (log)" if args.raw_time else "normalized wall-clock s (log)"
    time_sfx = "" if args.raw_time else " — normalized"
    _paired_scatter(paired, "k", "total tokens (log)", "fig6_paired_tokens",
                    "Covered both-success intersection — per-task tokens (RA vs baseline)")
    _paired_scatter(paired, "t", time_lbl, "fig7_paired_time",
                    f"Covered both-success intersection — per-task wall-clock{time_sfx} (RA vs baseline)")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
