# scripts/ — 脚本目录

按用途分四个子目录；三个高频入口留在根目录（被 docs / CI / `agents/flow_runner` / 兄弟脚本硬引用，且互相 import 依赖 `scripts/` 在 `sys.path`）。

## 根目录（核心入口，勿移）

| 脚本 | 作用 |
| --- | --- |
| `run_plan.py` | NL flow CLI 入口：合成 / 缓存 / 预览 / 确认 / 录屏 / 派发 `FlowRunner`。`--dry-run` `--yes` `--no-cache` `--record`。 |
| `run_benchmark_test.py` | 一键 A/B benchmark driver（relay vs MobileWorld）+ plan-only 分层。被 `manual_judge`/`reclassify_merge`/`phaseB_*` import。 |
| `run_mobileworld.py` | 跑单条 MobileWorld 真机 goal（含 MW server health/start/wait）。被 `agents/flow_runner._load_mw_driver` 按 `scripts/run_mobileworld.py` 硬加载。 |

## validate/ — 校验与跑前体检

| 脚本 | 作用 |
| --- | --- |
| `validate_manifests.py` | 校验 `manifests/` 全卡（schema + `prompt_template` 规则，**CI gate**）。 |
| `validate_relaybench.py` | 校验 `benchmark/relaybench_tasks.yaml` 结构与配比（**CI gate**）。 |
| `check_device_env.py` | 设备/IME/uiautomator/screencap/App 安装态体检。`--benchmark` `--apps` `--serial`。 |

## routes/ — 路由固化

| 脚本 | 作用 |
| --- | --- |
| `promote_routes.py` | 只读：把 trace 学到的高置信路由摆出供人工折回 capability matrix（绝不写 matrix）。 |
| `check_route_overlay.py` | route-solidification overlay 的自包含 smoke check。 |

## android/ — Android App 端工具

| 脚本 | 作用 |
| --- | --- |
| `gen_app_examples.py` | 从 `benchmark/` 生成 App 内置任务示例 `android/.../res/raw/examples.json`（50 条）。改基准后重跑。 |
| `diff_a11y_dump.py` | Spike B：对比 App 内 a11y 序列化 与真 `uiautomator dump` 的节点集 + text-hash 流。 |

## eval/ — 评测流水线（Phase B / 指标 / 画图 / 人工判）

| 脚本 | 作用 |
| --- | --- |
| `_phaseB_run.sh` | Phase B 真机 A/B supervisor（自愈 + 断点续跑）。 |
| `_phaseB_rerun.sh` | MW-only pass 结束后的定点 rerun supervisor。 |
| `_phaseB_mw_androiddaily.sh` | AndroidDaily 未测任务 MW 补测 supervisor（mw-only）。 |
| `phaseB_rerun_cases.py` | 定点 rerun 指定 (benchmark, system, task)，只产归一化时间 + token，不判成败。 |
| `phaseB_summary.py` | 把三个 benchmark 的 A/B 结果拍平成「每任务一行」的表。 |
| `reclassify_merge.py` | plan-only reclassification 合并（旧 covered 行 + 新分层）。 |
| `manual_judge.py` | Phase B VLM verdict 的人工 override 通道（CLI）。 |
| `manual_judge_web.py` | 同上的本地 web 复核 UI（stdlib http.server）。 |
| `aggregate_metrics.py` | 从 traj 日志聚合 token / 时间 / reply 长度（单 run）。 |
| `normalize_wall_clock.py` | 用 token-throughput 模型对 A/B 墙钟做归一化。 |
| `wall_clock_table.py` | 从归一化结果出「每任务配对」墙钟表（mw vs relay）。 |
| `calibrate_llm_throughput.py` | 微基准 LLM 网关，标定 per-token prefill/decode 成本。 |
| `plot_eval_figs.py` | 从真实 Phase B 数据渲染论文评测图集。 |
| `plot_summary_figs.py` | 从 `phaseB_summary.py` 的 summary.csv 画 fig1/6/7。 |

## _mw_probe/

`sitecustomize` shim，给 MobileWorld 子进程注入 LLM token 探针（见 `agents/llm/mw_llm_probe.py`），勿动。

> 移动脚本时记得：仓库根目录路径用 `Path(__file__).resolve().parents[2]` 解析（子目录深一层）。改引用后全仓 grep `scripts/<name>` 自查。
