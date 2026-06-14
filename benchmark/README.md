# RelayAgent Benchmark Suite

Task set built from the **capabilities** declared by each app's built-in agent in `manifests/*.yaml`. The design borrows from public mobile-GUI benchmarks:

- **MobileWorld** (Tongyi-MAI, ACL 2026): 201 tasks / 20 apps, with agent-user interaction + MCP augmentation —
  <https://github.com/Tongyi-MAI/MobileWorld>
- **AndroidWorld** (Google DeepMind, ICLR 2025): 116 parameterized tasks / 20 apps, each with explicit success criteria —
  <https://github.com/google-research/android_world>

Borrowed ideas: parameterized instructions (avoids memorization, keeps grounding realistic), an explicit `success` criterion per task, and `difficulty`/`category` tags.

> 中文版: [`README.zh.md`](README.zh.md)

## Files

| File | Content |
| --- | --- |
| [`relaybench_tasks.yaml`](relaybench_tasks.yaml) | **30 RelayBench tasks** (15 single-app + 15 cross-app, balanced across 10 manifest apps) |
| [`androiddaily_task_info.csv`](androiddaily_task_info.csv) | Cached task metadata for the external `stepfun-ai/AndroidDaily` benchmark |
| [`mobileworld_benchmark_task_info.csv`](mobileworld_benchmark_task_info.csv) | Cached task metadata for the external `Tongyi-MAI/MobileWorld` benchmark |

See [`docs/evaluation.md`](../docs/evaluation.md) for the full evaluation design covering all three benchmarks.

## RelayBench (`relaybench_tasks.yaml`)

**30-task** balanced benchmark: 15 single-app + 15 cross-app, covering **10 manifest apps** (each app appears 4-5 times across the whole set; exactly 3 times in the cross-app section).

| Type | Count | Purpose |
| --- | --- | --- |
| single-app | 15 | a single leg's capability reaches its intended end state (5 apps x 2 + 5 apps x 1) |
| cross-app | 15 | cross-app NL-flow orchestration (`run_plan.py`) |

**App disambiguation**: when multiple apps share a capability, the `instruction` must name the app (cross-app tasks are checked against `app_labels`).

```bash
# validate task-set structure / balance / disambiguation
uv run python scripts/validate/validate_relaybench.py

# smoke test (1 task per app, 10 by default)
uv run python scripts/run_benchmark_test.py --benchmark relaybench --dry-list

# full run (relay system only is recommended)
uv run python scripts/run_benchmark_test.py --benchmark relaybench --all --systems relay

# plan-only (no device execution)
uv run python scripts/run_benchmark_test.py --benchmark relaybench --plan-only
```

## Conventions

- **Taobao shopping is folded into Qwen**: the standalone `com.taobao.taobao` card has been removed (Taobao's built-in assistant *is* Qwen). Its `search_product` / `purchase_guidance` / `track_order` / `order_food` capabilities are now declared under the `com.aliyun.tongyi` manifest and routed through the Taobao backend, so these tasks use ids like `rb-sa-tongyi-shop-*` with `app: com.aliyun.tongyi`.
- **Safety semantics**: for any task with `handoff_required: true`, the agent must stop *before* an irreversible CTA (place order / pay / start navigation / open a document) and hand off — stopping right before the CTA counts as success; never actually complete the irreversible action.

## How to run

```bash
# single task (app_id + instruction)
uv run python -m agents.runtime.native_runner com.xingin.xhs "上海有什么值得一去的小众咖啡馆，推荐5家"

# shopping capabilities now live under Qwen (app_id=com.aliyun.tongyi)
uv run python -m agents.runtime.native_runner com.aliyun.tongyi "帮我找一台适合学生用的平板电脑，预算2000以内"

# natural-language flow (plan + per-step routing only, no device execution)
uv run python scripts/run_plan.py --dry-run "帮我找一台适合学生用的平板电脑，预算2000以内"
```

> Note: this task set always uses **manifest-canonical** ids — shopping capabilities live under `com.aliyun.tongyi`, and Ctrip uses `book_flight`/`book_hotel`/`book_train`.
