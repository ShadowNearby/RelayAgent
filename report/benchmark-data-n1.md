# RelayAgent 基准数据 — Round 1（n=1，2026-06-01）

> 第一轮 A/B 测试的完整记录，固化在 report 目录（`test-results/ab/` 是 gitignored，易丢）。
> 详细 traj/log 见 `test-results/ab/`。后续 n=3 + wall-clock 见 `benchmark-data-n3.md`（待补）。
> 重要平台事实：**淘宝闪购的内置 AI = 千问**，order_food 四档是同一下单后端的受控对照。

## 环境
- 设备：真机 `46180DLAQ004LW`；LLM 网关（`.env` 的 `LLM_BASE_URL`），model=`qwen`（VL）
- MobileWorld server :6800；RelayAgent 入口 `agents.native_runner`
- MW 原始 agent：`mw test --agent-type general_e2e`（逐步 VLM，每步带 3 张历史截图）
- **n=1**（每组单次，抖动未平均）

## 四档配置
| 代号 | 架构 |
|---|---|
| MW manual-UI | 逐步 VLM，**禁用 app 内 AI 助手**，手动原生 UI |
| MW general_e2e | 逐步 VLM，**用 app 内 AI 助手** |
| RA baseline | RelayAgent，`RELAY_PRECHECK=0 RELAY_SCRAPE=0`（≈优化前） |
| RA optimized | RelayAgent 默认（precheck + scrape 全开） |

## Test 1 — order_food（蜜雪冰城蜜桃四季春×3，温度糖度默认）
同一**淘宝闪购**下单后端（千问 = 闪购内置 AI），唯一变量是交互方式。

| 配置 | 路径 | 步数 | total tok | wall_s | vs RA opt |
|---|---|---|---:|---:|---:|
| MW manual-UI（无助手） | 淘宝闪购手动 | 9 | 75463 | 193 | 11.0× |
| MW general_e2e（用助手） | 千问助手 | 5 | 38081 | — | 5.6× |
| RA baseline | 千问 | 6 | 15364 | — | 2.2× |
| RA optimized | 千问 | 3 | 6851 | — | 1× |

- manual 首跑撞淘宝身份验证墙（6 步空转），过验证后重跑 9 步成功，结算页见「立即支付 ¥28.4」即停（未支付）。
- RA token 拆：optimized 6851（prompt 6620 / completion 231，3 VLM：router1+reply2，precheck skip 3）；baseline 15364（6 VLM：router1+reply5）。

## Test 2 — flow（上海交大附近新开好评餐厅 → 打车），xhs→amap 跨 app

| 子步 | MW manual-UI | MW general_e2e | RA baseline | RA optimized |
|---|---:|---:|---:|---:|
| discover (xhs) | 219061（23步/597s） | 56701（7步） | 17253 | 2827 |
| ride (amap) | 75634（9步/120s） | 47789（6步） | 8401 | 2809 |
| **合计** | **294695（717s）** | **104490** | **25654** | **5636** |

| 链路 | vs MW general_e2e | vs RA baseline |
|---|---:|---:|
| order_food | −82.0%（5.6×） | −55.4% |
| flow 合计 | −94.6%（18.5×） | −78.0% |

flow 合计 vs MW manual-UI：RA optimized 省 **98.1%**（52.3×）。

## 三个梯度归因
1. **MW manual-UI → general_e2e**：会用 app 自带 AI 助手就省一大截（flow 294695→104490，−65%；discover 23步→7步）。
2. **general_e2e → RA**：复用已登录助手（委派）+ manifest 零入口发现 + planner + uiautomator 点击（0 token）。flow discover 入口发现成本是 general_e2e 多花 2 步找「点点」的来源。
3. **RA baseline → optimized**：precheck 跳重复 done-poll + scrape 免 VLM 提取。

## wall-clock（仅 manual-UI 三条计时）
discover 597s / ride 120s / 淘宝闪购 193s。其余各档 n=1 未计时 → n=3 轮用 `RELAY_TIMING=1` 补。

## 已修阻塞
MW registry 选中抽象基类 `MCPAgent`（字母序）→ 实例化失败。修：`MCPAgent as _MCPAgentBase`。
commit `fe1682c`（分支 `ab-benchmark-and-registry-fix`）。
