# Dollar-Cost Restatement of Token Benchmark

**Scope:** T1 (order_food) and T2 (xhs→amap flow), four configs, per-task median.
**Date of analysis:** 2026-06-03.
**Model served:** `qwen3.5-27b` — Qwen3.5 27B native vision-language dense model (early-fusion multimodal, released Feb 2026), served via the OpenAI-compatible gateway configured in `.env` (`LLM_BASE_URL`) at no direct charge to the project. The gateway folds image/vision tokens into `prompt_tokens`; no separate vision price is reported by the API.

---

## 1. Pricing — source table

`qwen3.5-27b` is an open-weight model self-hosted by the lab. To translate tokens → dollars we use a **public-API proxy**: the closest documented hosted offering at a reputable aggregator.

| Provider | Proxy model name | Input $/M tok | Output $/M tok | Input:Output ratio | Source URL | Accessed |
|---|---|---:|---:|---:|---|---|
| **OpenRouter** | `qwen/qwen3.5-27b` | **$0.195** | **$1.560** | 1 : 8 | https://openrouter.ai/qwen/qwen3.5-27b | 2026-06-03 |
| SiliconFlow | `Qwen3.5-27B` | $0.250 | $2.000 | 1 : 8 | https://www.siliconflow.com/pricing | 2026-06-03 |
| Novita | `Qwen3.5-27B` | $0.300 | $2.400 | 1 : 8 | https://llm-stats.com/models/qwen3.5-27b | 2026-06-03 |

**Chosen proxy: OpenRouter `qwen/qwen3.5-27b` at $0.195/M input, $1.56/M output** (the lowest listed public price, and the most commonly cited). All three providers quote the same 1:8 input:output price ratio.

The lab gateway is free; the dollar figures below represent the equivalent market cost if the same workload were run on a public API. No CNY conversion is needed (all three providers quote USD).

### Notes on model identity
OpenRouter describes `qwen/qwen3.5-27b` as "Qwen3.5 27B native vision-language Dense model," confirming it is the same family (multimodal, 27B dense, early-fusion) as the lab-served model. The model ID and description match what the lab gateway exposes as `qwen`. This is the same model used for all benchmark runs; no VLM-specific surcharge is listed separately (images are priced as input tokens, consistent with how the lab gateway reports them).

---

## 2. Per-config dollar cost — T1 (order_food)

Computed as: **cost = prompt_tokens × $0.195/M + completion_tokens × $1.56/M**

Splits used: median-representative run per config (MW manual-UI n=1; others n=3 median total).

| Configuration | Total tokens | Prompt | Completion | Prompt % | **$/task** | **$× vs RA opt** | Token× vs RA opt |
|---|---:|---:|---:|---:|---:|---:|---:|
| MW manual-UI (no assistant) | 75,463 | 74,669 | 794 | 98.9% | $0.01580 | **16.1×** | 18.9× |
| MW general_e2e (uses assistant) | 77,347 | 76,453 | 894 | 98.8% | $0.01630 | **16.6×** | 19.4× |
| RA baseline | 9,585 | 9,403 | 182 | 98.1% | $0.00212 | **2.2×** | 2.4× |
| **RA optimized** | **3,987** | **3,837** | **150** | **96.2%** | **$0.00098** | **1×** | 1× |

All costs are sub-cent. RA optimized: **$0.00098 per task** (~0.1 cent). General_e2e: **$0.01630** (~1.6 cents).

### Interpretation of the dollar gap vs token gap

The dollar multiplier (16.6×) is **smaller** than the token multiplier (19.4×) for general_e2e vs RA optimized. The quantitative reason:

- RA optimized is 96.2% prompt / 3.8% completion; general_e2e is 98.8% prompt / 1.2% completion.
- With a 1:8 price ratio (input:output), completion tokens are relatively expensive. RA optimized has a *higher completion fraction* than general_e2e (3.8% vs 1.2%), so its effective $/token is slightly elevated relative to what a flat price would predict. This makes RA optimized's baseline cost slightly higher per token, compressing the $× vs the token×.
- Concretely: general_e2e completion is only 8.6% of its cost despite being 1.2% of its tokens. RA optimized completion is 23.8% of its cost despite being 3.8% of its tokens.
- Net: token× = 19.4× → $× = 16.6×, a **14% compression** in the multiplier (the dollar gap is 1.17× smaller than the token gap). Same direction for manual-UI: tok× 18.9× → $× 16.1× (15% compression). For RA baseline: tok× 2.4× → $× 2.2× (10% compression).

The compression exists — it is real — but it is modest (~14–15%) because all configs are so heavily prompt-dominated (~97–99%) that the 1:8 price ratio barely shifts the weighted average price per token.

**Important caveat on the "dollar gap is smaller" claim:** the 1:8 input:output ratio does make the $× moderately smaller than tok×, but the primary argument for why the *dollar* gap is smaller than tok× in practice is **prompt caching**. All ~97–99% prompt tokens are dominated by images and fixed context, which are cacheable on most providers. If cached prompt tokens are charged at ~10–25% of full input price (typical cache-hit rates on OpenRouter/DashScope), the effective per-task cost of the high-volume configs collapses much further relative to RA optimized, which issues only 1 image and minimal reusable context. The table above shows full (non-cached) prices and should be read as an upper bound on the absolute cost for high-volume configs.

---

## 3. T2 flow — cross-check (total tokens only)

No prompt/completion split is readily available for T2. We approximate using T1-derived prompt fractions as a proxy (manual-UI 98.9%, general_e2e 98.8%, RA baseline 98.1%, RA optimized 96.2%). This is a rough estimate; treat as order-of-magnitude only.

| Configuration | Total tokens | $/task (est.) | **$× vs RA opt** | Token× vs RA opt |
|---|---:|---:|---:|---:|
| MW manual-UI | 294,695 | ~$0.0619 | ~28.9× | 34.0× |
| MW general_e2e | 95,296 | ~$0.0201 | ~9.4× | 11.0× |
| RA baseline | 31,174 | ~$0.0069 | ~3.2× | 3.6× |
| **RA optimized** | **8,662** | **~$0.0021** | **1×** | 1× |

The T2 pattern is consistent: the dollar multiplier is modestly smaller than the token multiplier (T2: ~28.9× vs 34.0× for manual-UI; ~9.4× vs 11.0× for general_e2e), for the same reason — RA optimized has a slightly higher completion fraction, elevating its effective $/token baseline.

**Flat-price check:** Both OpenRouter and SiliconFlow charge the same 1:8 input:output ratio. If the ratio were 1:1 (flat pricing), the $× would equal tok× exactly, and the "dollar gap is smaller" argument would rest *entirely* on cacheability, not on a price-tier difference. At 1:8, the price-tier effect contributes a ~14–15% multiplier compression on top of whatever caching provides.

---

## 4. Ready-to-paste text

### 4a. Snippet for §8.2 — insert after "Token composition" paragraph (after the sentence ending "…flag a $-normalized restatement as future work (§8.9).")

> **Dollar restatement (§8.9 item 2 resolved).** Using OpenRouter's public price for `qwen/qwen3.5-27b` ($0.195/M input, $1.56/M output, accessed 2026-06-03) as a proxy for the lab-served model, per-task costs are: RA optimized **$0.00098**, RA baseline $0.00212 (2.2×), general_e2e $0.01630 (16.6×), manual-UI $0.01580 (16.1×). The dollar multiplier (16.6×) is modestly smaller than the token multiplier (19.4×): all configs are 97–99% prompt tokens, but RA optimized has a slightly higher completion fraction (3.8%) than general_e2e (1.2%), so at the 1:8 input:output price ratio its effective $/token baseline is slightly elevated, compressing the gap by ~14%. More importantly, the ~97–99% prompt-heavy workload is highly cacheable; at typical cache-hit rates the absolute dollar gap between high-volume configs and RA optimized would widen further. These are full (non-cached) prices. The lab gateway is free; figures represent equivalent market cost. See `report/cost-dollar-analysis.md` for full methodology.

### 4b. One-line update for §8.9 item 2

Replace the final two sentences of §8.9 item 2 (currently: "Two strengthenings remain future work: a $-normalized restatement using public Qwen-VL input/output pricing, and a frugal-input rerun…") with:

> The $-normalized restatement is now available (see `report/cost-dollar-analysis.md`): at OpenRouter public pricing for `qwen/qwen3.5-27b` ($0.195/M in, $1.56/M out), RA optimized costs $0.00098/task vs $0.01630 for general_e2e (16.6×, versus the 19.4× token gap) — the ~14% compression reflects RA optimized's slightly higher completion fraction at the 1:8 price ratio; the caching argument for a further gap remains open. The frugal-input rerun (`HISTORY_N_IMAGES=1`) remains future work.

---

## 5. Caveats

1. **Proxy pricing.** `qwen3.5-27b` is self-hosted by the lab at zero direct cost. OpenRouter is used as a proxy because it is a reputable multi-provider aggregator that lists the exact model by name and confirms it is a native vision-language model. The listed price ($0.195/M in, $1.56/M out) includes a stated "35% discount" on OpenRouter's page; the pre-discount rate would be ~$0.30/M in, $2.40/M out — consistent with Novita's list price. SiliconFlow charges $0.25/$2.00. The three providers bracket a narrow range; the conclusions are unchanged across any of them.

2. **Image tokens folded into prompt.** The lab gateway reports vision/image tokens inside `prompt_tokens` with no separate line item. The OpenRouter price applies uniformly to the entire prompt count, which is also consistent with how the lab's token metering works. No separate vision surcharge has been applied.

3. **No prompt caching applied.** The $/task figures are full (non-cached) prices. In practice the prompt-heavy, image-dominated tokens of general_e2e and manual-UI are the most cacheable; cached prompt prices are typically 10–25% of list price. A caching-adjusted comparison would make the dollar gap even larger than shown.

4. **T2 prompt/completion split estimated.** T2 costs are estimated using T1-derived prompt fractions (98.9/98.8/98.1/96.2%) as proxies. The T2 figures are order-of-magnitude cross-checks, not independently measured splits.

5. **FX.** All prices are in USD. No CNY conversion was needed; all three providers quote USD.
