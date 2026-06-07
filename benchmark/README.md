# RelayAgent 任务集（benchmark）

以 `manifests/*.yaml` 里各 App 内置 Agent 声明的 **capability** 为来源构造的单 App 任务集。借鉴公开 mobile GUI benchmark 的范式：

- **MobileWorld** (Tongyi-MAI, ACL 2026)：201 任务 / 20 app，含 agent-user 交互 + MCP 增强 —
  <https://github.com/Tongyi-MAI/MobileWorld>
- **AndroidWorld** (Google DeepMind, ICLR 2025)：116 参数化任务 / 20 app，每任务带显式 success criteria —
  <https://github.com/google-research/android_world>

借鉴点：instruction 参数化（防记忆 + 逼真 grounding）、每任务显式 `success` 判据、标注 `difficulty`/`category`。

## 文件

| 文件 | 内容 |
| --- | --- |
| [`single_app_tasks.yaml`](single_app_tasks.yaml) | **50 条单 App 任务**（单 app + 单 capability，1-step 即可完成） |

> 单 App 任务用于验证每张 manifest card 的 capability 是否能稳定到达预期终态。

## single_app_tasks.yaml 一览

- 覆盖全部 **6 个 App / 27 个 capability**（每个 capability ≥1 条）。
- category：`info_qa` 26 · `transaction` 16 · `content_gen` 6 · `navigation` 2
- difficulty：`easy` 18 · `medium` 23 · `hard` 9
- 每 App 任务数：千问 17 · 高德 8 · 携程 7 · WPS 7 · 小红书 6 · 微信 5

> **淘宝购物能力已并入千问**：原独立的 `com.taobao.taobao` 卡片已下线（淘宝内置助手本身*就是*千问），
> 其 `search_product` / `purchase_guidance` / `track_order` / `order_food` 现声明在
> `com.aliyun.tongyi` manifest 下、经 Taobao 后端路由。故这些任务的 id 形如 `tongyi-shop-*`，`app` 为千问。

字段含义见 YAML 头部注释。**安全语义**：`handoff_required=true` 的任务，agent 须停在
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
