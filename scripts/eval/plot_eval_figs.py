#!/usr/bin/env python3
"""Render the paper's evaluation figure set from REAL Phase B data.

Reads ``traj_logs/phaseB/<bench>/results.jsonl`` (per-task, per-system A/B rows)
plus the plan-only tier classification (docs/evaluation.zh.md §11). Benchmarks
without a results.jsonl yet (e.g. MobileWorld A/B not run) are dropped from the
A/B figures but kept in fig1's coverage stratification.

Figures (see docs/eval_figs/):
  fig1_coverage.{png,pdf}      — per-benchmark covered/fallback stratification (plan-only)
  fig2_efficiency.{png,pdf}    — covered stratum: relay vs baseline on time/tokens/steps
  fig4_per_app.{png,pdf}       — per-app dumbbell: relay vs baseline (success/time/tokens)
  fig6_paired_tokens.{png,pdf} — both-success intersection: per-task RA vs baseline tokens
  fig7_paired_time.{png,pdf}   — both-success intersection: per-task RA vs baseline wall-clock
  fig5_outcome_table.{png,pdf} — 2x2 outcome matrix (RA × baseline success/fail), per bench

Run:  uv run python scripts/eval/plot_eval_figs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "docs" / "eval_figs"
DATA_DIR = REPO_ROOT / "traj_logs" / "phaseB"
LEAKS_FILE = DATA_DIR / "leak_suspects.txt"


def _load_leak_ids() -> set[str]:
    """Task ids contaminated by the pre-fix MW state-leak (MW ran after relay with
    no device reset and read relay's leftover screen). Active only with
    --exclude-leaks; ids come from leak_suspects.txt (one per line, '#' comments
    and inline '# ...' notes stripped)."""
    if "--exclude-leaks" not in sys.argv or not LEAKS_FILE.exists():
        return set()
    ids: set[str] = set()
    for line in LEAKS_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return ids


EXCLUDE_IDS = _load_leak_ids()

# ── fixed palette (Okabe–Ito-ish; relay/baseline consistent across ALL figures) ──
RELAY = "#0072B2"      # blue   — RelayAgent
BASELINE = "#D55E00"   # vermillion — MobileWorld general_e2e
TIER_COLORS = {        # coverage tiers: covered = the win stratum
    "covered": "#1B7837",
    "foundation_fallback": "#A6DBA0",
    "mixed": "#9970AB",
    "mw": "#C2A5CF",
    "invalid": "#D9D9D9",
}
TIER_LABEL = {
    "covered": "covered (specialized route)",
    "foundation_fallback": "foundation fallback",
    "mixed": "mixed (MW + specialized)",
    "mw": "MW fallback (= baseline)",
    "invalid": "invalid / error",
}

mpl.rcParams.update({
    "figure.dpi": 130,
    "savefig.dpi": 200,
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.6,
})

# benchmark display name -> phaseB results dir
BENCH_DIR = {
    "RelayBench": "relaybench",
    "AndroidDaily": "androiddaily",
    "MobileWorld": "mobileworld",
}
ALL_BENCHES = ["RelayBench", "AndroidDaily", "MobileWorld"]  # fig1 (plan-only, complete)

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ REAL DATA loader — reads traj_logs/phaseB/<bench>/results.jsonl (per-task, ║
# ║ per-system A/B rows) + the plan-only tier classification from §11 of       ║
# ║ docs/evaluation.zh.md. Benchmarks without a results.jsonl yet (MobileWorld ║
# ║ A/B not run) are dropped from the A/B figures but kept in fig1.            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# fig1: plan-only leg-kind classification (docs/evaluation.zh.md §11, 2026-06-10).
# Complete for all three benchmarks even though the A/B run is partial.
TIERS = {
    "RelayBench":   {"covered": 27, "foundation_fallback": 3,  "mixed": 0, "mw": 0},
    "AndroidDaily": {"covered": 71, "foundation_fallback": 19, "mixed": 2, "mw": 143},
    "MobileWorld":  {"covered": 61, "foundation_fallback": 10, "mixed": 0, "mw": 90},
}

# nominal app (results.jsonl `app`) -> (romanized label, RA has a manifest?). Merges
# the variant spellings (Ctrip/Qunar/12306 each written two ways). Package names from
# RelayBench are mapped too; fig4 only uses AndroidDaily but the map is shared.
APP_NORM = {
    # ── AndroidDaily (Chinese names) ──
    "高德地图": ("Amap", True), "携程": ("Ctrip", True), "携程旅行": ("Ctrip", True),
    "淘宝": ("Taobao", True), "小红书": ("XHS", True),
    "铁路12306": ("12306", False), "12306": ("12306", False), "飞猪": ("Fliggy", False),
    "同程旅行": ("Tongcheng", False), "去哪儿旅行": ("Qunar", False), "去哪儿": ("Qunar", False),
    "京东": ("JD", False), "滴滴出行": ("DiDi", False), "美团": ("Meituan", False),
    # ── RelayBench (package names) ──
    "com.aliyun.tongyi": ("Tongyi", True), "com.autonavi.minimap": ("Amap", True),
    "ctrip.android.view": ("Ctrip", True), "com.google.android.apps.bard": ("Gemini", True),
    "com.tencent.mm": ("WeChat", True), "com.xingin.xhs": ("XHS", True),
    "cn.wps.moffice_eng": ("WPS", True), "com.booking": ("Booking", True),
    "com.reddit.frontpage": ("Reddit", True), "com.microsoft.copilot": ("Copilot", True),
}


def _is_success(rec: dict | None) -> bool:
    return bool(rec) and (rec.get("verdict") or {}).get("status") == "success"


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else 0.0


def _load_bench(bench: str) -> dict[str, dict] | None:
    """results.jsonl -> {task_id: {"relay": rec, "mw": rec}}; None if no file."""
    path = DATA_DIR / BENCH_DIR[bench] / "results.jsonl"
    if not path.exists():
        return None
    by_id: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["id"] in EXCLUDE_IDS:
            continue
        by_id.setdefault(r["id"], {})[r["system"]] = r
    return by_id


def _covered_app(rec_pair: dict) -> bool:
    """True if the task's nominal app is one RA has a specialized manifest for.
    Phase B ran only covered-TIER ids, but some are cross-app tasks whose nominal
    app (Fliggy/12306/JD/DiDi/…) has no RA manifest — they route to another app's
    capability. We keep only RA-manifest apps so the figures show the clean
    covered-app stratum (mixed / fallback-app tasks dropped)."""
    raw_app = (rec_pair.get("relay") or rec_pair.get("mw") or {}).get("app") or ""
    return APP_NORM.get(raw_app, ("", False))[1]


# load every benchmark with data, then keep only RA-manifest-app (covered) tasks.
RAW = {}
for _b in ALL_BENCHES:
    _by_id = _load_bench(_b)
    if _by_id is None:
        continue
    RAW[_b] = {tid: d for tid, d in _by_id.items() if _covered_app(d)}
AB_BENCHES = [b for b in ALL_BENCHES if RAW.get(b)]


def _covered_aggregate() -> dict:
    """fig2: per-bench efficiency on the BOTH-SUCCESS intersection — only tasks
    where RA *and* baseline both succeeded, so the two systems are averaged over
    the SAME task set (paired). This deliberately drops MW timeouts / early-stop
    failures (and RA failures); success% is still reported on the full covered
    set as a separate gate. Phase B ran exactly the covered id-set."""
    out: dict[str, dict] = {}
    for b in AB_BENCHES:
        by_id = RAW[b]
        both = [d for d in by_id.values()
                if _is_success(d.get("relay")) and _is_success(d.get("mw"))]
        out[b] = {"_n_both": len(both)}
        for sys, key in (("relay", "relay"), ("mw", "baseline")):
            allrecs = [d[sys] for d in by_id.values() if sys in d]
            out[b][key] = dict(
                success=round(100 * sum(_is_success(r) for r in allrecs) / max(len(allrecs), 1)),
                time=_mean([d[sys]["elapsed_s"] for d in both]),
                tokens=_mean([d[sys]["total_tokens"] for d in both]),
                steps=_mean([d[sys]["steps"] for d in both]),
            )
    return out


def _by_app(bench: str = "AndroidDaily") -> dict:
    """fig4: group results by normalized app. success% per system on all rows of
    that app; time/tokens on the BOTH-SUCCESS intersection within the app (same
    paired set as fig2). covered flag = nominal app has an RA manifest."""
    by_id = RAW.get(bench) or {}
    groups: dict[str, dict] = {}
    for d in by_id.values():
        raw_app = (d.get("relay") or d.get("mw") or {}).get("app") or "?"
        label, covered = APP_NORM.get(raw_app, (raw_app, False))
        g = groups.setdefault(label, dict(covered=covered, n=0,
                                          relay=dict(_s=[], _t=[], _k=[]),
                                          base=dict(_s=[], _t=[], _k=[])))
        g["n"] += 1
        both_ok = _is_success(d.get("relay")) and _is_success(d.get("mw"))
        for sys, slot in (("relay", "relay"), ("mw", "base")):
            r = d.get(sys)
            if not r:
                continue
            g[slot]["_s"].append(1 if _is_success(r) else 0)
            if both_ok:  # time/tokens only on the paired both-success set
                g[slot]["_t"].append(r["elapsed_s"])
                g[slot]["_k"].append(r["total_tokens"])
    # collapse accumulators into scalars (time/tokens completed-only; fall back to 0)
    out: dict[str, dict] = {}
    for label, g in groups.items():
        out[label] = dict(covered=g["covered"], n=g["n"],
                          relay=dict(s=round(100 * _mean(g["relay"]["_s"])),
                                     t=_mean(g["relay"]["_t"]) or 1,
                                     k=_mean(g["relay"]["_k"]) or 1),
                          base=dict(s=round(100 * _mean(g["base"]["_s"])),
                                    t=_mean(g["base"]["_t"]) or 1,
                                    k=_mean(g["base"]["_k"]) or 1))
    return out


def _is_covered(relay_rec: dict) -> bool:
    """RA actually ran a specialized route (no MobileWorld-fallback leg). At runtime
    a task can drop into MW fallback even though it was classified covered plan-only;
    those legs are named ``*mw_fallback*``. Empty legs == RA produced no real leg."""
    legs = relay_rec.get("relay_legs") or []
    if not legs:
        return False
    return not any("mw_fallback" in str(l.get("step", "")) for l in legs)


def _paired(bench: str) -> list[dict]:
    """per-task join for fig5/6/7: tasks where BOTH systems produced a row."""
    recs = []
    for tid, d in (RAW.get(bench) or {}).items():
        if "relay" not in d or "mw" not in d:
            continue
        ra, mw = d["relay"], d["mw"]
        recs.append(dict(
            task=tid, ra_ok=_is_success(ra), base_ok=_is_success(mw),
            covered=_is_covered(ra),
            ra_t=ra["elapsed_s"], base_t=mw["elapsed_s"],
            ra_k=ra["total_tokens"], base_k=mw["total_tokens"],
        ))
    return recs


COVERED = _covered_aggregate()
BY_APP = _by_app("AndroidDaily")
PAIRED = {b: _paired(b) for b in AB_BENCHES}

# ── end data loader ──


def _save(fig, stem: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(OUT_DIR / f"{stem}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {OUT_DIR / stem}.png / .pdf")


def fig1_coverage() -> None:
    """Horizontal stacked bars: covered/fallback share per benchmark."""
    fig, ax = plt.subplots(figsize=(8.2, 2.8))
    order = ["covered", "foundation_fallback", "mixed", "mw"]
    y = np.arange(len(ALL_BENCHES))[::-1]  # top-to-bottom = ALL_BENCHES order
    for i, b in enumerate(ALL_BENCHES):
        tiers = TIERS[b]
        total = sum(tiers.values()) or 1
        left = 0.0
        for t in order:
            w = 100.0 * tiers.get(t, 0) / total
            ax.barh(y[i], w, left=left, color=TIER_COLORS[t],
                    edgecolor="white", height=0.62,
                    label=TIER_LABEL[t] if i == 0 else None)
            if w >= 7:
                ax.text(left + w / 2, y[i], f"{w:.0f}%", ha="center", va="center",
                        color="white" if t in ("covered", "mw") else "#333",
                        fontsize=9, fontweight="bold")
            left += w
        ax.text(101, y[i], f"N={total}", va="center", ha="left", fontsize=9, color="#555")
    ax.set_yticks(y, ALL_BENCHES)
    ax.set_xlim(0, 108)
    ax.set_xlabel("share of tasks (%)")
    ax.set_title("Coverage stratification: where RelayAgent routes to a specialized agent",
                 fontsize=11, loc="left")
    ax.grid(axis="y", visible=False)
    ax.legend(ncol=2, fontsize=8.5, loc="lower center", bbox_to_anchor=(0.5, -0.55),
              frameon=False)
    _save(fig, "fig1_coverage")


def fig2_efficiency() -> None:
    """Covered stratum: relay vs baseline on time / tokens / steps (3 panels)."""
    metrics = [("time", "wall-clock (s, mean)"), ("tokens", "total tokens (mean)"),
               ("steps", "steps (mean)")]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
    x = np.arange(len(AB_BENCHES))
    w = 0.36
    for ax, (key, ylab) in zip(axes, metrics):
        rv = [COVERED[b]["relay"][key] for b in AB_BENCHES]
        bv = [COVERED[b]["baseline"][key] for b in AB_BENCHES]
        ax.bar(x - w / 2, rv, w, color=RELAY, label="RelayAgent")
        ax.bar(x + w / 2, bv, w, color=BASELINE, label="MW general_e2e")
        # speedup/saving annotation over each benchmark
        for xi, (r, b) in enumerate(zip(rv, bv)):
            if r > 0:
                ax.text(xi, max(r, b) * 1.02, f"{b / r:.1f}×", ha="center",
                        va="bottom", fontsize=8.5, color="#222")
        xlabels = [f"{b}\n(n={COVERED[b]['_n_both']} both-OK)" for b in AB_BENCHES]
        ax.set_xticks(x, xlabels, rotation=0, fontsize=9)
        ax.set_ylabel(ylab, fontsize=10)
        ax.margins(y=0.18)
    # success% as a shared caption row (computed on the FULL covered set, the gate
    # under which the both-success efficiency comparison above is valid)
    succ = " · ".join(
        f"{b}: RA {COVERED[b]['relay']['success']}% / base "
        f"{COVERED[b]['baseline']['success']}%" for b in AB_BENCHES)
    fig.suptitle("Covered stratum, both-success intersection — RA vs baseline "
                 "(lower is better; n× = saving)", fontsize=12, x=0.5, y=1.04)
    fig.text(0.5, -0.10, "success rate on full covered set (judge):  " + succ,
             ha="center", fontsize=8.6, color="#444")
    axes[0].legend(fontsize=9, loc="upper left", frameon=False)
    fig.tight_layout()
    _save(fig, "fig2_efficiency")


def fig4_per_app(by_app: dict, bench: str = "AndroidDaily") -> None:
    """Dumbbell per app: relay vs baseline on success/time/tokens within one bench.

    Apps sorted covered-first then by task count. A dot per system, joined by a
    line; the gap IS the effect. Covered-app rows are bold + carry the gap — on
    fallback apps the two dots collapse together (RA == baseline by construction).
    """
    apps = sorted(by_app, key=lambda a: (not by_app[a]["covered"], -by_app[a]["n"]))
    y = np.arange(len(apps))[::-1]
    panels = [("s", "success rate (%)", False), ("t", "wall-clock (s, mean)", True),
              ("k", "total tokens (mean)", True)]
    fig, axes = plt.subplots(1, 3, figsize=(12, 0.42 * len(apps) + 1.6), sharey=True)
    for ax, (key, xlab, logx) in zip(axes, panels):
        for yi, a in zip(y, apps):
            rv, bv = by_app[a]["relay"][key], by_app[a]["base"][key]
            ax.plot([bv, rv], [yi, yi], color="#bbb", lw=2, zorder=1)
            ax.scatter(bv, yi, color=BASELINE, s=42, zorder=2)
            ax.scatter(rv, yi, color=RELAY, s=42, zorder=3)
        if logx:
            ax.set_xscale("log")
        ax.set_xlabel(xlab, fontsize=10)
        ax.grid(axis="y", visible=False)
    # y labels: bold covered apps, append (n)
    labels = [f"{a} ({by_app[a]['n']})" for a in apps]
    axes[0].set_yticks(y, labels)
    for tick, a in zip(axes[0].get_yticklabels(), apps):
        tick.set_fontweight("bold" if by_app[a]["covered"] else "normal")
        tick.set_color("#1B7837" if by_app[a]["covered"] else "#555")
    # legend (system dots) + covered/fallback note
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], marker="o", color="w", markerfacecolor=RELAY,
                      markersize=8, label="RelayAgent"),
               Line2D([0], [0], marker="o", color="w", markerfacecolor=BASELINE,
                      markersize=8, label="MW general_e2e")]
    axes[2].legend(handles=handles, fontsize=9, loc="lower right", frameon=False)
    fig.suptitle(f"Per-app comparison — {bench}  (covered-app stratum; "
                 f"RA-manifest apps only)", fontsize=12, y=1.0)
    fig.tight_layout()
    _save(fig, "fig4_per_app")


def _paired_scatter(metric: str, ylabel: str, stem: str, title: str) -> None:
    """Per-task paired scatter on the BOTH-SUCCESS intersection, one panel per bench.

    Keeps only COVERED both-success tasks: RA ran a specialized route (no MW
    fallback leg) AND both systems succeeded — so MW timeouts / early-stop
    failures and RA failures are dropped. Sorted along x by the baseline/RA ratio
    (largest RA win on the left). At each x two vertically-aligned points — RA
    (blue) and baseline (orange) — joined by a thin connector so the per-task gap
    reads directly. x carries no tick labels; it is just rank. y is log.
    """
    rak, bak = f"ra_{metric}", f"base_{metric}"
    fig, axes = plt.subplots(1, len(AB_BENCHES), figsize=(4 * len(AB_BENCHES), 3.7),
                             sharey=True, squeeze=False)
    axes = axes[0]
    for ax, b in zip(axes, AB_BENCHES):
        # covered both-success: RA ran a specialized route (no MW fallback) AND both
        # systems succeeded. Drops MW timeouts / early-stop failures and RA failures.
        recs = [r for r in PAIRED[b]
                if r["ra_ok"] and r["base_ok"] and r["covered"] and r[rak] and r[bak]]
        recs.sort(key=lambda r: r[bak] / r[rak], reverse=True)
        ra = np.array([r[rak] for r in recs])
        ba = np.array([r[bak] for r in recs])
        xs = np.arange(len(recs))
        for xi, rv, bv in zip(xs, ra, ba):
            # gray = RA cheaper; red = RA dearer (per-task gap, direction only)
            ax.plot([xi, xi], [rv, bv], color="#e8a0a0" if rv > bv else "#cfcfcf",
                    lw=0.7, zorder=1)
        # filled = that system succeeded; hollow = it failed (still timed/counted)
        ra_fc = [RELAY if r["ra_ok"] else "none" for r in recs]
        ba_fc = [BASELINE if r["base_ok"] else "none" for r in recs]
        ax.scatter(xs, ba, s=18, zorder=3, facecolors=ba_fc, edgecolors=BASELINE,
                   linewidths=1.0, label="MW general_e2e")
        ax.scatter(xs, ra, s=18, zorder=3, facecolors=ra_fc, edgecolors=RELAY,
                   linewidths=1.0, label="RelayAgent")
        ax.set_yscale("log")
        ax.set_title(f"{b}  (n={len(recs)})", fontsize=10)
        ax.set_xticks([])
        ax.set_xlabel("covered both-success tasks\n"
                      "← sorted by baseline/RA ratio", fontsize=8.5)
        med = float(np.median(ba / ra))
        win = float(np.mean(ba > ra) * 100)
        ax.text(0.97, 0.96, f"median {med:.1f}×\nRA cheaper {win:.0f}%",
                transform=ax.transAxes, ha="right", va="top", fontsize=8.6, color="#222",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#ccc", alpha=0.9))
    axes[0].set_ylabel(ylabel)
    axes[0].legend(fontsize=8.5, loc="lower left", frameon=False)
    fig.suptitle(title, fontsize=12, y=1.02)
    fig.tight_layout()
    _save(fig, stem)


def _outcomes(recs: list[dict]) -> tuple[int, int, int, int]:
    """(both succeed, RA-only, baseline-only, both fail)."""
    both = sum(r["ra_ok"] and r["base_ok"] for r in recs)
    ra_only = sum(r["ra_ok"] and not r["base_ok"] for r in recs)
    base_only = sum(not r["ra_ok"] and r["base_ok"] for r in recs)
    neither = sum(not r["ra_ok"] and not r["base_ok"] for r in recs)
    return both, ra_only, base_only, neither


def fig5_outcome_table() -> None:
    """2x2 success-outcome matrix (RA × baseline), one row per benchmark + TOTAL.

    Columns are the four cells of the confusion matrix: both succeed / RA succeeds
    while baseline fails / baseline succeeds while RA fails / both fail. The two
    off-diagonal cells are the discordant pairs (what a McNemar test would use).
    """
    cols = ["both succeed", "RA ✓ / base ✗", "base ✓ / RA ✗", "both fail", "N"]
    rows, cells, totals = [], [], [0, 0, 0, 0]
    for b in AB_BENCHES:
        both, ra_only, base_only, neither = _outcomes(PAIRED[b])
        n = both + ra_only + base_only + neither
        for j, v in enumerate((both, ra_only, base_only, neither)):
            totals[j] += v
        rows.append(b)
        cells.append([f"{both}\n({100*both/n:.0f}%)", f"{ra_only}\n({100*ra_only/n:.0f}%)",
                      f"{base_only}\n({100*base_only/n:.0f}%)", f"{neither}\n({100*neither/n:.0f}%)",
                      str(n)])
    tn = sum(totals)
    rows.append("TOTAL")
    cells.append([f"{totals[0]}\n({100*totals[0]/tn:.0f}%)", f"{totals[1]}\n({100*totals[1]/tn:.0f}%)",
                  f"{totals[2]}\n({100*totals[2]/tn:.0f}%)", f"{totals[3]}\n({100*totals[3]/tn:.0f}%)",
                  str(tn)])

    # console echo (handy when iterating without opening the PNG)
    print("  outcome matrix (RA × baseline):")
    print(f"    {'bench':<14}{'both':>8}{'RA-only':>9}{'base-only':>11}{'both-fail':>11}{'N':>6}")
    for b, c in zip(rows, cells):
        flat = [x.split('\n')[0] for x in c]
        print(f"    {b:<14}{flat[0]:>8}{flat[1]:>9}{flat[2]:>11}{flat[3]:>11}{flat[4]:>6}")

    fig, ax = plt.subplots(figsize=(8.6, 0.62 * len(rows) + 1.2))
    ax.axis("off")
    tbl = ax.table(cellText=cells, rowLabels=rows, colLabels=cols,
                   cellLoc="center", rowLoc="center", loc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9.5)
    tbl.scale(1, 2.2)
    col_bg = ["#D9EAD3", "#CFE2F3", "#FCE5CD", "#F4CCCC", "#FFFFFF"]  # green/blue/orange/red/white
    for (r, c), cell in tbl.get_celld().items():
        if r == 0:  # header
            cell.set_text_props(fontweight="bold")
            cell.set_facecolor("#404040")
            cell.get_text().set_color("white")
        elif c >= 0:
            cell.set_facecolor(col_bg[c])
        if r == len(rows):  # TOTAL row
            cell.set_text_props(fontweight="bold")
    ax.set_title("Success-outcome matrix — RelayAgent vs baseline (per task)",
                 fontsize=12, loc="left", pad=14)
    _save(fig, "fig5_outcome_table")


def main() -> int:
    print(f"rendering eval figures -> {OUT_DIR}")
    if EXCLUDE_IDS:
        print(f"  --exclude-leaks ON: dropped {len(EXCLUDE_IDS)} state-leak task(s) "
              f"from {LEAKS_FILE.name}")
    print(f"  A/B data present for: {', '.join(AB_BENCHES) or '(none)'}"
          f"  ·  fig1 tiers cover: {', '.join(ALL_BENCHES)}")
    fig1_coverage()
    if AB_BENCHES:
        fig2_efficiency()
        if BY_APP:
            fig4_per_app(BY_APP)
        _paired_scatter("k", "total tokens (log)", "fig6_paired_tokens",
                        "Covered both-success intersection — per-task tokens (RA vs baseline)")
        _paired_scatter("t", "wall-clock s (log)", "fig7_paired_time",
                        "Covered both-success intersection — per-task wall-clock (RA vs baseline)")
        fig5_outcome_table()
    else:
        print("  no results.jsonl found under traj_logs/phaseB — only fig1 rendered")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
