#!/usr/bin/env python3
"""Normalize A/B wall-clock against a token-throughput model of LLM call time.

Why
---
The shared in-lab LLM gateway has highly variable per-call latency (queueing):
the same call can take 4s or 160s depending on load. Raw subprocess wall-clock
(``results.jsonl`` ``elapsed_s``) therefore mixes two things:

  - **stable device/adb time** — screenshots (~1.5s), settle sleeps, taps. Real,
    reproducible, what we actually want to compare.
  - **noisy LLM time** — dominated by gateway queueing, not by the agent.

Because ``mw`` issues many more / larger calls than ``relay``, it also absorbs
disproportionately more queue noise, which unfairly inflates its wall-clock. To
compare the *systems* (not the gateway's mood), we replace each call's measured
latency with a deterministic model time and rebuild wall-clock:

    wall_norm = elapsed_s - sum(actual_llm_latency) + sum(model_llm_time)

The device/sleep remainder (``elapsed_s - sum(actual_llm_latency)``) is kept
verbatim; only the LLM portion is re-priced.

Model
-----
Each call's compute time is split into prefill (prompt tokens) and decode
(completion tokens):

    model_time = alpha * p_eff + beta * c          # p_eff = prompt - cached

with the **prefill/decode == 1:1 time assumption** (``--decode-prefill-ratio``,
default 1.0): over the whole pooled dataset total decode time equals total
prefill time, i.e. ``beta * sum(c) = W * alpha * sum(p_eff)`` →
``beta = alpha * W * sum(p_eff) / sum(c)``. That leaves a single free scale
``alpha``, fit robustly as ``median(t_i / x_i)`` where
``x_i = p_eff_i + (W*P/C) * c_i`` (median shrugs off the queue outliers).

``--anchor`` picks the percentile of ``t/x`` used for the scale: ``median``
(typical-load, default), ``p25`` or ``p10`` (closer to unloaded compute).

Usage
-----
    scripts/eval/normalize_wall_clock.py traj_logs/phaseB/*/results.jsonl
    scripts/eval/normalize_wall_clock.py traj_logs/phaseB/mobileworld/results.jsonl \
        --anchor p10 --decode-prefill-ratio 1.0

Writes ``<dir>/results_normalized.jsonl`` (each row gains ``elapsed_s_raw``,
``llm_time_actual_s``, ``llm_time_norm_s``, ``elapsed_s_norm``; ``elapsed_s`` is
left untouched) and prints the fitted constants + a raw-vs-norm summary. The
fitted constants + per-system stats are also dropped at ``wall_norm_fit.json``
next to the first input (or ``--fit-out``).
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def _call_latency(c: dict[str, Any]) -> float | None:
    """mw probe carries ``elapsed_s``; relay token_usage carries ``latency_s``."""
    v = c.get("elapsed_s")
    if v is None:
        v = c.get("latency_s")
    return None if v is None else float(v)


def usable_calls(row: dict[str, Any]) -> list[tuple[float, int, int, int]]:
    """(latency_s, prompt, completion, cached) for non-errored, fully-typed calls."""
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


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def fit_throughput(pool: list[tuple[float, int, int, int]], *, w: float, anchor: str):
    """Return (alpha, beta, P_eff, C, x_of, diag). Raises on empty / degenerate pool."""
    P = sum(p - cached for _, p, _, cached in pool)   # effective prefill tokens
    C = sum(comp for _, _, comp, _ in pool)           # decode tokens
    if P <= 0 or C <= 0:
        raise ValueError(f"degenerate token totals: P_eff={P} C={C}")
    k = w * P / C                                     # decode weight in x

    def x_of(p: int, comp: int, cached: int) -> float:
        return (p - cached) + k * comp

    ratios = sorted(t / x_of(p, comp, cached)
                    for t, p, comp, cached in pool if x_of(p, comp, cached) > 0)
    picks = {"median": statistics.median(ratios),
             "p25": _percentile(ratios, 0.25),
             "p10": _percentile(ratios, 0.10)}
    if anchor not in picks:
        raise ValueError(f"unknown anchor {anchor!r}")
    alpha = picks[anchor]
    beta = alpha * k
    diag = {"n_calls": len(pool), "sum_prefill_eff_tokens": P, "sum_decode_tokens": C,
            "decode_weight_k": k, "ratio_quantiles": {a: picks[a] for a in picks}}
    return alpha, beta, x_of, diag


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path, help="one or more results.jsonl")
    ap.add_argument("--anchor", choices=("median", "p25", "p10"), default="median",
                    help="percentile of t/x used as the throughput scale (default median)")
    ap.add_argument("--decode-prefill-ratio", type=float, default=1.0, dest="w",
                    help="assumed total decode-time / prefill-time (default 1.0 == 1:1)")
    ap.add_argument("--fit-file", type=Path, default=None,
                    help="calibrate_llm_throughput.py JSON: use MEASURED gamma/alpha/beta "
                         "(model_time = gamma + alpha*p_eff + beta*c) instead of the 1:1 fit")
    ap.add_argument("--fit-out", type=Path, default=None,
                    help="where to write the fit JSON (default: wall_norm_fit.json by input)")
    args = ap.parse_args()

    files = [p for p in args.results if p.exists()]
    missing = [str(p) for p in args.results if not p.exists()]
    for m in missing:
        print(f"[warn] skip missing {m}")
    if not files:
        print("[error] no existing results.jsonl given")
        return 2

    # Load every row, tagged with its source file so we can rewrite per-file.
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for f in files:
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                loaded.append((f, json.loads(line)))

    pool = [c for _, r in loaded for c in usable_calls(r)]
    if not pool:
        print("[error] no usable llm_calls found across inputs")
        return 2

    if args.fit_file:
        # Measured constants from calibrate_llm_throughput.py: gamma + a*p_eff + b*c.
        cal = json.loads(args.fit_file.read_text())
        gamma = float(cal.get("gamma_s_per_call") or 0.0)
        alpha = float(cal["alpha_s_per_prefill_tok"])
        beta = float(cal["beta_s_per_decode_tok"])

        def model_time(p: int, comp: int, cached: int) -> float:
            return gamma + alpha * (p - cached) + beta * comp

        fit_meta = {"source": "calibrated", "fit_file": str(args.fit_file),
                    "gamma_s_per_call": gamma, "alpha_s_per_prefill_tok": alpha,
                    "beta_s_per_decode_tok": beta,
                    "prefill_tok_per_s": (1 / alpha) if alpha > 0 else None,
                    "decode_tok_per_s": 1 / beta}
        print(f"calibrated fit ({args.fit_file}): gamma={gamma:.3f}s/call  "
              f"prefill {1/alpha:,.0f} tok/s (alpha={alpha:.3e})  "
              f"decode {1/beta:,.1f} tok/s (beta={beta:.3e})")
    else:
        alpha, beta, x_of, diag = fit_throughput(pool, w=args.w, anchor=args.anchor)

        def model_time(p: int, comp: int, cached: int) -> float:
            return alpha * x_of(p, comp, cached)

        fit_meta = {"source": "ratio_1to1", "assumption_decode_prefill_ratio": args.w,
                    "anchor": args.anchor, "alpha_s_per_prefill_tok": alpha,
                    "beta_s_per_decode_tok": beta, "prefill_tok_per_s": 1 / alpha,
                    "decode_tok_per_s": 1 / beta, "pool_diag": diag}
        print(f"pooled calls={diag['n_calls']}  prefill_eff={diag['sum_prefill_eff_tokens']:,}tok  "
              f"decode={diag['sum_decode_tokens']:,}tok  (P/C={diag['sum_prefill_eff_tokens']/diag['sum_decode_tokens']:.1f})")
        print(f"assumption: decode_time/prefill_time = {args.w}  anchor={args.anchor}")
        print(f"fit: prefill {1/alpha:,.0f} tok/s (alpha={alpha:.3e} s/tok)   "
              f"decode {1/beta:,.1f} tok/s (beta={beta:.3e} s/tok)")

    # Rewrite each row with normalized wall-clock.
    per_sys_raw: dict[str, list[float]] = {}
    per_sys_norm: dict[str, list[float]] = {}
    out_rows: dict[Path, list[dict[str, Any]]] = {f: [] for f in files}
    print(f"\n{'id':30s} {'sys':5s} {'raw_s':>8s} {'llm_act':>8s} {'llm_norm':>8s} {'wall_norm':>9s}")
    for f, r in loaded:
        cs = usable_calls(r)
        llm_act = sum(t for t, _, _, _ in cs)
        llm_norm = sum(model_time(p, comp, cached) for _, p, comp, cached in cs)
        raw = float(r.get("elapsed_s") or 0.0)
        wall_norm = raw - llm_act + llm_norm
        if wall_norm < 0:
            print(f"  [warn] {r.get('id')} {r.get('system')}: negative wall_norm "
                  f"({wall_norm:.1f}); actual LLM time exceeds elapsed (parallelism?) — clamping")
            wall_norm = llm_norm
        out = dict(r)
        out["elapsed_s_raw"] = raw
        out["llm_time_actual_s"] = round(llm_act, 3)
        out["llm_time_norm_s"] = round(llm_norm, 3)
        out["elapsed_s_norm"] = round(wall_norm, 3)
        out_rows[f].append(out)
        sysn = r.get("system", "?")
        per_sys_raw.setdefault(sysn, []).append(raw)
        per_sys_norm.setdefault(sysn, []).append(wall_norm)
        print(f"{str(r.get('id'))[:30]:30s} {sysn:5s} {raw:8.1f} {llm_act:8.1f} {llm_norm:8.1f} {wall_norm:9.1f}")

    print(f"\n{'system':8s} {'n':>3s} {'raw_med':>9s} {'norm_med':>9s} {'raw_mean':>9s} {'norm_mean':>9s}")
    sys_stats: dict[str, Any] = {}
    for sysn in sorted(per_sys_raw):
        rw, nm = per_sys_raw[sysn], per_sys_norm[sysn]
        sys_stats[sysn] = {"n": len(rw),
                           "raw_median": statistics.median(rw), "norm_median": statistics.median(nm),
                           "raw_mean": statistics.fmean(rw), "norm_mean": statistics.fmean(nm)}
        s = sys_stats[sysn]
        print(f"{sysn:8s} {len(rw):3d} {s['raw_median']:9.1f} {s['norm_median']:9.1f} "
              f"{s['raw_mean']:9.1f} {s['norm_mean']:9.1f}")

    for f, rows in out_rows.items():
        dst = f.with_name("results_normalized.jsonl")
        dst.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        print(f"\nwrote {dst}  ({len(rows)} rows)")

    fit_out = args.fit_out or files[0].with_name("wall_norm_fit.json")
    fit_out.write_text(json.dumps({
        "inputs": [str(f) for f in files], **fit_meta, "per_system": sys_stats,
    }, indent=2, ensure_ascii=False))
    print(f"wrote {fit_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
