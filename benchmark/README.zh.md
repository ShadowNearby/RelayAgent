# RelayAgent 任务集（benchmark）

以 `manifests/*.yaml` 里各 App 内置 Agent 声明的 **capability** 为来源构造的任务集。借鉴公开 mobile GUI benchmark 的范式：

- **MobileWorld** (Tongyi-MAI, ACL 2026)：201 任务 / 20 app，含 agent-user 交互 + MCP 增强 —
  <https://github.com/Tongyi-MAI/MobileWorld>
- **AndroidWorld** (Google DeepMind, ICLR 2025)：116 参数化任务 / 20 app，每任务带显式 success criteria —
  <https://github.com/google-research/android_world>

借鉴点：instruction 参数化（防记忆 + 逼真 grounding）、每任务显式 `success` 判据、标注 `difficulty`/`category`。

> English: [`README.md`](README.md)

## 文件

| 文件 | 内容 |
| --- | --- |
| [`relaybench_tasks.yaml`](relaybench_tasks.yaml) | **30 条 RelayBench**（15 single + 15 cross，10 App 均衡） |
| [`androiddaily_task_info.csv`](androiddaily_task_info.csv) | 外部基准 `stepfun-ai/AndroidDaily` 的缓存任务元数据 |
| [`mobileworld_benchmark_task_info.csv`](mobileworld_benchmark_task_info.csv) | 外部基准 `Tongyi-MAI/MobileWorld` 的缓存任务元数据 |

三套基准的完整评测设计见 [`docs/evaluation.zh.md`](../docs/evaluation.zh.md)。

## RelayBench（`relaybench_tasks.yaml`）

**30 条**均衡基准：15 single-app + 15 cross-app，覆盖 **10 个 manifest App**（每 App 全库出现 4–5 次；cross 段每 App 恰好 3 次）。

| 类型 | 条数 | 用途 |
| --- | --- | --- |
| single-app | 15 | 单 leg 能力到达终态（5 App×2 + 5 App×1） |
| cross-app | 15 | NL flow 跨 App 编排（`run_plan.py`） |

**App 消歧**：多 App 共享 capability 时，`instruction` 必须点名 App（cross 任务用 `app_labels` 校验）。

```bash
# 校验任务集结构 / 均衡 / 消歧
uv run python scripts/validate_relaybench.py

# 冒烟（每 App 1 条，默认 10 条）
uv run python scripts/run_benchmark_test.py --benchmark relaybench --dry-list

# 全量（建议仅 relay）
uv run python scripts/run_benchmark_test.py --benchmark relaybench --all --systems relay

# 只看规划
uv run python scripts/run_benchmark_test.py --benchmark relaybench --plan-only
```

## 约定

- **淘宝购物能力已并入千问**：原独立的 `com.taobao.taobao` 卡片已下线（淘宝内置助手本身*就是*千问），
  其 `search_product` / `purchase_guidance` / `track_order` / `order_food` 现声明在
  `com.aliyun.tongyi` manifest 下、经 Taobao 后端路由。故这些任务的 id 形如 `rb-sa-tongyi-shop-*`，`app` 为 `com.aliyun.tongyi`。
- **安全语义**：`handoff_required=true` 的任务，agent 须停在
  不可逆 CTA（下单/支付/开始导航/打开文档）**之前**交还——“停在 CTA 前”即视为成功，不真下单/支付。

## 怎么跑

```bash
# 单条（app_id + 指令）
uv run python -m agents.native_runner com.xingin.xhs "上海有什么值得一去的小众咖啡馆，推荐5家"

# 购物类现走千问（app_id=com.aliyun.tongyi）
uv run python -m agents.native_runner com.aliyun.tongyi "帮我找一台适合学生用的平板电脑，预算2000以内"

# 自然语言 flow（只看规划和每步路由）
uv run python scripts/run_plan.py --dry-run "帮我找一台适合学生用的平板电脑，预算2000以内"
```

> 注：本任务集一律使用 **manifest-canonical** id；购物能力归到 `com.aliyun.tongyi`，
> 携程使用 `book_flight/book_hotel/book_train`。
