#!/usr/bin/env python3
"""Microbenchmark the LLM gateway to calibrate per-token prefill/decode cost.

Replaces the ad-hoc "prefill == decode time" (1:1) assumption in
``normalize_wall_clock.py`` with *measured* constants, using the standard serving
decomposition:

    call_latency ≈ gamma + alpha * prompt_tokens + beta * completion_tokens
                   └fixed┘   └─ prefill (TTFT) ─┘   └──── decode (TPOT) ────┘

We measure each term directly via streaming:
  - **TTFT** (time-to-first-token) is prefill-dominated. Sweeping prompt length
    and regressing TTFT on the *measured* prompt_tokens gives slope ``alpha``
    (s / prefill-tok) and intercept ``gamma`` (fixed per-call overhead: network
    RTT + scheduling + queue floor).
  - **TPOT** (time-per-output-token) is decode. From a long-output run,
    ``beta = (latency - TTFT) / (completion_tokens - 1)``, independent of prompt
    length. Reported as the median over reps.

Run this at LOW gateway load (the intercept absorbs a queue *floor*, but heavy
contention inflates everything). Repeat (``--reps``) and we take per-bucket
medians so transient spikes don't bend the fit.

Usage
-----
    scripts/eval/calibrate_llm_throughput.py                       # defaults
    scripts/eval/calibrate_llm_throughput.py --reps 5 \
        --prefill-sizes 500,2000,5000,9000 --decode-tokens 400 \
        --out traj_logs/phaseB/llm_calib.json

Writes a fit JSON with ``gamma_s_per_call``, ``alpha_s_per_prefill_tok``,
``beta_s_per_decode_tok`` (+ raw samples). Feed it to the normalizer:

    scripts/eval/normalize_wall_clock.py traj_logs/phaseB/*/results.jsonl \
        --fit-file traj_logs/phaseB/llm_calib.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
import uuid
from pathlib import Path

from openai import OpenAI

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents.runtime.runtime_config import resolve_llm_config  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _stream_once(client: OpenAI, model: str, prompt: str, max_tokens: int):
    """One streaming call. Returns (ttft_s, total_s, prompt_tok, completion_tok)."""
    t0 = time.perf_counter()
    ttft = None
    usage = None
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
        stream=True,
        stream_options={"include_usage": True},
    )
    for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if ttft is None and getattr(delta, "content", None):
            ttft = time.perf_counter() - t0
    total = time.perf_counter() - t0
    p = usage.prompt_tokens if usage else None
    c = usage.completion_tokens if usage else None
    return ttft, total, p, c


def _filler_prompt(target_tokens: int) -> str:
    """~target_tokens of UNIQUE filler so the gateway can't prefix/response-cache it.

    The gateway prefix-caches identical prefixes (TTFT collapses to ~0 on a repeat),
    which would understate prefill cost. Production agent calls report cached_tokens=0,
    so we measure the *uncached* path: a fresh nonce up front busts any prefix match,
    and random-number body busts full-content caching.
    """
    nonce = uuid.uuid4().hex
    body = " ".join(str(random.randint(0, 999999)) for _ in range(max(1, target_tokens)))
    return f"[req {nonce}] Below is random data; reply with exactly the word OK.\n{body}"


def _queue_floor(vals: list[float], q: float) -> tuple[float, int]:
    """Lower-quantile estimate of the *unloaded* time + count of rejected high outliers.

    Gateway latency noise is one-sided: queueing only ever ADDS time, never
    subtracts. So the true compute time at a fixed token count sits at the lower
    envelope, and every value far above it is a queue-inflated outlier to drop.
    We take the q-quantile (default p20) as the floor estimate and report how many
    samples exceeded 2x it (the discarded queue spikes).
    """
    s = sorted(vals)
    floor = s[min(len(s) - 1, int(q * len(s)))]
    rejected = sum(1 for v in s if v > 2 * floor)
    return floor, rejected


def calibrate(client: OpenAI, model: str, *, reps: int, prefill_sizes: list[int],
              decode_tokens: int, sleep_s: float, bucket_q: float) -> dict:
    # ---- prefill sweep: TTFT vs prompt_tokens (tiny output) ----
    prefill_samples = []  # (prompt_tok, ttft_s)
    print(f"prefill sweep: sizes={prefill_sizes} reps={reps}")
    for size in prefill_sizes:
        for r in range(reps):
            prompt = _filler_prompt(size)  # fresh nonce+body each rep → no cache hit
            ttft, total, p, c = _stream_once(client, model, prompt, max_tokens=4)
            if ttft is None or p is None:
                print(f"  size~{size} rep{r}: no TTFT/usage, skip")
                continue
            if ttft < 1e-3 + p * 1e-5:  # implausibly fast for the size → cache hit, drop
                print(f"  prompt_tok={p} ttft={ttft:.3f}s — cache hit, skip")
                continue
            prefill_samples.append((p, ttft))
            print(f"  prompt_tok={p:6d} ttft={ttft:6.3f}s (total={total:.3f} out={c})")
            time.sleep(sleep_s)

    # slope/intercept from the queue-floor (low quantile) TTFT per size bucket, then
    # OLS. One-sided queue noise → the floor, not the median, is the true prefill time.
    buckets: dict[int, list[float]] = {}
    for p, ttft in prefill_samples:
        buckets.setdefault(round(p, -2), []).append(ttft)  # bucket to nearest 100 tok
    pts = []
    dropped_prefill = 0
    for bk, v in sorted(buckets.items()):
        floor, rej = _queue_floor(v, bucket_q)
        dropped_prefill += rej
        pts.append((bk, floor))
        print(f"  bucket {bk:6d}tok: floor(p{int(bucket_q*100)})={floor:.3f}s "
              f"med={statistics.median(v):.3f}s n={len(v)} dropped={rej}")
    if len(pts) < 2:
        raise SystemExit("need >=2 distinct prompt-size buckets to fit prefill slope")
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    n = len(xs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    alpha = sxy / sxx                  # s per prefill token
    gamma = my - alpha * mx            # fixed per-call overhead
    if alpha < 0:
        print(f"  [warn] negative prefill slope ({alpha:.2e}); noisy/low-load issue")

    # ---- decode: TPOT from a long-output run ----
    print(f"\ndecode sweep: max_tokens={decode_tokens} reps={reps}")
    tpots = []
    decode_samples = []
    for r in range(reps):
        # Unique nonce per rep so a cache-hit can't return decode in ~0s.
        long_prompt = (f"[req {uuid.uuid4().hex}] Count upward in words starting from one, "
                       "comma-separated, and do not stop until cut off: one, two, three,")
        ttft, total, p, c = _stream_once(client, model, long_prompt, max_tokens=decode_tokens)
        if ttft is None or not c or c < 2:
            print(f"  rep{r}: insufficient output (c={c}), skip")
            continue
        tpot = (total - ttft) / (c - 1)
        if tpot < 1e-4:  # decode finished in ~0s == cache hit, not a real generation
            print(f"  rep{r}: tpot~0 (cache hit, out={c}), skip")
            continue
        tpots.append(tpot)
        decode_samples.append({"prompt_tok": p, "completion_tok": c, "ttft_s": ttft,
                               "total_s": total, "tpot_s": tpot})
        print(f"  out_tok={c:4d} ttft={ttft:.3f}s decode={total-ttft:6.3f}s tpot={tpot*1000:6.1f}ms/tok")
        time.sleep(sleep_s)
    if not tpots:
        raise SystemExit("no usable decode samples")
    beta, dropped_decode = _queue_floor(tpots, bucket_q)   # queue-floor TPOT
    print(f"  TPOT floor(p{int(bucket_q*100)})={beta*1000:.1f}ms med={statistics.median(tpots)*1000:.1f}ms "
          f"n={len(tpots)} dropped={dropped_decode}")

    return {
        "model": model,
        "bucket_quantile": bucket_q,
        "gamma_s_per_call": gamma,
        "alpha_s_per_prefill_tok": alpha,
        "beta_s_per_decode_tok": beta,
        "prefill_tok_per_s": (1 / alpha) if alpha > 0 else None,
        "decode_tok_per_s": 1 / beta,
        "prefill_fit_points": [{"prompt_tok": x, "ttft_floor_s": y} for x, y in pts],
        "decode_samples": decode_samples,
        "n_prefill_samples": len(prefill_samples),
        "n_prefill_outliers_dropped": dropped_prefill,
        "n_decode_samples": len(tpots),
        "n_decode_outliers_dropped": dropped_decode,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--prefill-sizes", default="500,2000,5000,9000",
                    help="comma-separated target prompt token sizes")
    ap.add_argument("--decode-tokens", type=int, default=400, help="max_tokens for decode run")
    ap.add_argument("--sleep", type=float, default=1.0, help="sleep between calls (keep load low)")
    ap.add_argument("--bucket-quantile", type=float, default=0.2, dest="bucket_q",
                    help="lower quantile per size bucket as the unloaded floor (default 0.2); "
                         "rejects queue-inflated high outliers")
    ap.add_argument("--out", type=Path, default=Path("traj_logs/phaseB/llm_calib.json"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    _, base_url, api_key, model = resolve_llm_config(
        ENV_FILE, model=args.model, base_url=args.base_url, api_key=args.api_key)
    client = OpenAI(base_url=base_url, api_key=api_key or "empty")
    sizes = [int(s) for s in args.prefill_sizes.split(",") if s.strip()]

    fit = calibrate(client, model, reps=args.reps, prefill_sizes=sizes,
                    decode_tokens=args.decode_tokens, sleep_s=args.sleep, bucket_q=args.bucket_q)

    print("\n==== calibrated fit ====")
    print(f"gamma (fixed/call):   {fit['gamma_s_per_call']:.3f} s")
    pps = fit["prefill_tok_per_s"]
    print(f"alpha (prefill):      {fit['alpha_s_per_prefill_tok']:.3e} s/tok"
          + (f"  ({pps:,.0f} tok/s)" if pps else ""))
    print(f"beta  (decode/TPOT):  {fit['beta_s_per_decode_tok']:.3e} s/tok"
          f"  ({fit['decode_tok_per_s']:,.1f} tok/s)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fit, indent=2, ensure_ascii=False))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
