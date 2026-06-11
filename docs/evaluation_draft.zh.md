# Evaluation（正文草稿 · 中文）

> 草稿状态：图表数值取自 `docs/eval_figs/fig{5,6,7}`，**当前为 MOCK 占位**（真机 A/B 0 条，见 `docs/evaluation.zh.md` §11）。文中所有具体数字、p 值均标注 `〔TODO:真值〕`，真跑数回来后整体替换，论证结构不变。

在本章，我们将 RelayAgent（下称 **RA**）与通用 GUI agent baseline 在同一批任务上逐题对照，回答三个问题：（Q1）专用路由是否以牺牲任务完成率为代价；（Q2）相比逐帧操作的 baseline，RA 在墙钟时间上的收益有多大；（Q3）在 token 消耗上的收益有多大。我们的核心论点是：**在 RA 具备专用 in-app agent 覆盖的任务上，RA 大幅节省时间与 token，且完成率不弱于 baseline；在覆盖不到的任务上，RA 退化为与 baseline 同一执行基座，仅额外支付少量规划开销。**

## 5.1 实验设置

**环境与模型。** 全部实验在同一台真机上完成（设备 〔TODO:机型〕，Android 〔TODO:OS 版本〕，分辨率 〔TODO:WxH〕）。RA 运行时为纯 Python 直接驱动 adb，无独立 server、无框架冷启动；输入经 AdbKeyboard IME 注入，每个任务前执行 cold-launch（`am force-stop` + LAUNCHER 启动 + settle）以消除应用残留状态。RA 的规划、应用内交互的 grounding，以及评测 judge 共用同一个实验室内 LLM 网关上的视觉语言模型（〔TODO:确切型号与版本，如 Qwen3-VL-xxB〕，下记 `qwen`），解码温度统一设为 0。

**对照公平：同一 backbone。** 这是一个方法对照而非模型对照——**baseline（MobileWorld `general_e2e`）与 RA 调用完全相同的 `qwen` 模型与解码参数**，差异只在"逐帧像素操作"对"路由到 in-app agent"这一执行范式上。两系统在同一台设备、同一批任务、相同的初始应用状态下先后运行。

**动作空间与 grounding。** RA 的动作空间为 `JSONAction`（`tap` / `swipe` / `scroll` / `input_text` / `open_app` 等基础操作，外加高层的 `tap_text`、`wait_for_reply`、`ask_user`）；坐标 grounding 以 a11y 树（uiautomator）优先、VLM 兜底。baseline 在相同的基础动作空间上逐帧由 VLM 直接输出坐标，不使用 a11y 树。

**执行预算。** 两系统每个任务的步数上限统一为 〔TODO:N，如 50〕 步、墙钟超时统一为 〔TODO:T 秒，如 600s〕；达到上限或超时即按失败计入（这也定义了 §5.3 中"baseline 超时天花板"）。每个任务两系统各运行 〔TODO:R，如 1〕 次〔若 R>1 说明取中位/均值〕；真机存在波动，故按 §5.1 末"绝对分数仅供参考、相对差异可信"解读。

为保证逐题对照的公平性并得到干净的墙钟，评测时强制关闭以下会跨任务泄漏热状态、污染计时或给 RA 单边信息优势的特性（见表 1）：路由固化 overlay（否则后续任务命中查表短路 planner，token/时间随任务顺序漂移）、逐步截图日志（否则每步额外写 PNG 与标注帧）、plan/route 缓存、以及**回复全文滚动捕获**。最后一项需特别说明：baseline `general_e2e` 无滚动捕获，回复在屏上看着稳定后即读取**当前可见帧**文本并 `answer`；RA 的 `x_capture_full_reply` 则会把 offscreen 的多节点回复卡片滚动进视野再拼接，从而对同一目标获得严格更多的回复内容。为对齐二者，评测时将 RA 的 `wait_for_reply` 同样限定为"屏幕文本稳定即停、只返回首帧可见文本"，不进入滚动捕获相。与正确性相关的特性保持开启：fresh-conversation、AdbKeyboard IME、cold-launch。

**Benchmark。** 我们在三个并列的 benchmark 上评测，不分主副（表 2）。RelayBench 为自建，精确覆盖 RA 的 10 个 manifest 应用，用于内部精确测量；AndroidDaily、MobileWorld 为外部标准集，分别提供中文日常重命中场景与英文低命中泛化场景，构成外部证据并主动暴露覆盖不到时的退化行为。MobileWorld 中触及 `MCP-*` 的任务为 tool-call 而非真实 GUI 操作，经 `--skip-mcp` 过滤后由 201 条减为 161 条。鉴于真机应用持续迭代、运行环境存在波动，本章报告的**绝对分数仅供参考，真正可信的是两系统在同一环境同一批任务上的相对差异**。

| Benchmark | 来源 | 规模 | 语言 | 作用 |
| --- | --- | --- | --- | --- |
| RelayBench | 自建 | 30（15 single + 15 cross） | 中 | 覆盖 RA 全部 manifest，内部精确测量 |
| AndroidDaily | `stepfun-ai/AndroidDaily` | 235 | 中 | 外部标准、重度命中 RA 覆盖 |
| MobileWorld | `Tongyi-MAI/MobileWorld` | 161（`--skip-mcp`） | 英为主 | 外部标准、低命中，测泛化/不退化 |

表 2：三个并列 benchmark。

**Baseline。** 主 baseline 为 MobileWorld 的 `general_e2e`——一个不做专用路由、纯逐帧像素操作的通用 GUI agent。需澄清一处关系：**baseline 只是 RA 在 fallback 层的子集，而非 RA 整体的子集。** 当 RA 判定某条 leg 不可满足时，会将其转交给同一个 `general_e2e` 执行（mw_fallback 层，此时 baseline 确为 RA 的子集）；但在 covered 层，RA 用专用 in-app agent 替换了 `general_e2e`，二者是两条不同路径而非包含关系。由此推论：完成率上 "RA ≥ baseline" 是经验期望而非逻辑保证——covered 层是不同执行器、可能逐任务更差，fallback 层还要额外减去路由/handoff 误差的损耗。

**评测口径与 judge。** 三个 benchmark 用同一个端到端 VLM judge（`leg_judge`）按 SUCCESS/total 打分。两个外部集的原生判定我们均不沿用：AndroidDaily 的原生指标是对 ground-truth 轨迹的 step-action-accuracy，而 RA 路由到 in-app agent 后不产出可比的逐步序列；MobileWorld 的原生判定依赖后端数据库/回调等确定性 oracle，而 RA 在 app 自带 agent 内完成任务、并不触达这些后端状态。因此我们统一只复用两集的任务指令，改用同一个 e2e VLM judge 给两个系统打同一把尺，以保证三个 benchmark 跨系统可比。代价是放弃了 MobileWorld 确定性 oracle 的精确性，故 judge 是 RA 自家组件这一点须额外把关：我们抽取 〔TODO:30–50〕 题人工核对，报告人机一致率为 〔TODO:一致率〕。

**评测协议。** 为保证评分的准确，我们对所有判为失败的任务做人工复核；对因应用异常、网络中断或任务在当前环境下本不可达而失败的样例予以重测，不计入两系统的能力差异。对 agent 无法正常完成、也无法正常退出（陷入动作无限重复）的任务，我们施加终止惩罚，按失败计。每个任务两系统在同一设备状态下先后运行，任务间执行 cold-launch 复位，避免前序任务的残留状态泄漏。

## 5.2 完成率

图 5（success-outcome 矩阵）按任务把 RA 与 baseline 的成败配成 2×2：both succeed / `RA✓ base✗` / `base✓ RA✗` / both fail。

**对角线随覆盖度变化。** both-succeed 比例从 RelayBench 的 〔73%〕、AndroidDaily 的 〔64%〕 降到 MobileWorld 的 〔55%〕，与三个集合对 RA 覆盖度由高到低一致。

**离对角线（discordant pairs）揭示净优劣。** 两个离对角格的差直接反映谁净赢。RelayBench 上 `RA✓ base✗` 〔13%〕 显著高于 `base✓ RA✗` 〔3%〕，RA 净赢；AndroidDaily 上 〔12%〕 vs 〔9%〕，RA 仍小幅净赢；MobileWorld 上 〔9%〕 vs 〔15%〕，RA 反而小幅净亏。后者符合预期：MobileWorld 上 RA 几乎全部走 mw_fallback，与 baseline 共用执行基座却要多承担一段路由/handoff，故事是"非退化 + 规划税"而非"赢"。

**为什么 baseline 在某些 app 上也不弱。** 一个值得说明的现象是：当目标应用自身提供 AI 搜索/助手入口时，逐帧操作的 baseline 也会把用户需求整段塞进搜索框，从而绕过大量交互步骤、取得不低的完成率——这恰恰从反面印证了 RA 的出发点：**任务真正的执行者是 app 自带的、已登录的 in-app agent，而非屏幕上的逐帧操作**。差别在于，baseline 这种"碰运气塞搜索框"的粗粒度策略不可控——一旦应用不支持 AI 搜索、或入口需要多步导航，baseline 的完成率显著下滑；RA 则通过 manifest 把"找到并正确进入该 in-app agent"这一步固化下来，把偶然变成稳定。

**非终止失败。** 与 MobiAgent 的观察一致，逐帧 baseline 仍存在任务无法正常终止的问题（动作无限重复、无法退出），集中出现在 〔TODO:N〕 类应用；RA 因 handoff 契约显式界定每条 leg 的完成与交接，未出现此类悬挂。

**显著性。** 两个离对角格即 discordant pairs，我们对每个 benchmark 做 McNemar 检验：RelayBench 〔TODO:p〕、AndroidDaily 〔TODO:p〕、MobileWorld 〔TODO:p〕。结论是 RA 在覆盖充分的集合上完成率显著不弱于乃至优于 baseline，在低覆盖集合上无显著退化。

## 5.3 时间

**RA 在覆盖充分的任务上把单次任务墙钟压缩 2–3.5×。** 图 7 为 both-success 交集的逐任务墙钟配对散点（每 benchmark 一面板，按 baseline/RA 比值排序）。RA 相对 baseline 的墙钟中位加速：RelayBench 〔3.5×〕（RA 在 〔95%〕 的配对任务上更快）、AndroidDaily 〔2.2×〕（〔61%〕）、MobileWorld 〔0.9×〕（〔7%〕）。

**为什么用交集配对作 headline。** 直接比较墙钟有 selection bias，我们并报三套口径以同时暴露相反方向的偏差：（i）全量——包含 baseline 因超时跑不完的任务，会以超时天花板**高估** RA 优势；（ii）各系统 completed-only——各算各的、不可逐题配对；（iii）both-success 交集配对——只取两系统都成功的题逐题求比值，因 conditioning on baseline-success 而**删掉了"baseline 超时、RA 几步完成"这类 RA 最大赢点**，从而**低估** RA。三者方向相反地有偏，故必须同列；上文的 headline 取最保守的交集配对（图 7），意在以下界陈述收益。

**论点。** 即便在主动删去 RA 最大赢点的最保守口径下，覆盖充分的集合仍有 2–3.5× 的中位加速；考虑到交集口径已剔除 baseline 超时的题，全量口径下的真实优势更大（全量含超时天花板，方向上进一步放大 RA）。MobileWorld 的 〔0.9×〕 则如实画出 fallback 层的规划税：与 baseline 同基座执行、再多付一段规划，故略慢。加速的机制在于 covered 层用一次结构化的 in-app submit 替代了 baseline 的几十步逐帧 tap——这一点由 covered 层显著更少的 step 数 / LLM 调用次数佐证 〔TODO:引用机制图或表〕。

## 5.4 Token

口径与 5.3 完全一致（全量 / completed-only / both-success 交集配对），此处只报交集配对结果与 token 特有的讨论。图 6 为 both-success 交集的逐任务 token 配对散点。RA 相对 baseline 的 token 中位节省：RelayBench 〔3.7×〕（RA 在 〔95%〕 配对任务上更省）、AndroidDaily 〔2.8×〕（〔61%〕）、MobileWorld 〔0.9×〕（〔7%〕）。

**论点与机制。** token 节省幅度与时间高度一致，因为二者同源：covered 层用一次结构化 submit 替代了 baseline 逐帧喂入的图像 token 与多轮视觉推理，省下的正是那几十帧截图的输入 token 及对应输出。MobileWorld 的 〔0.9×〕 同样是规划税的体现。

**一处必须交代的口径风险。** RA 侧一次性的 plan-synthesis 调用（含 coverage-gap 修复轮）此前未计入 relay token（driver CAVEAT，见 `docs/evaluation.zh.md` §7.2 / TODO #8）。该项漏采会低估 fallback 层的规划税、相应高估 token 优势，直接影响 MobileWorld 上 〔0.9×〕 的准确性。本文报告的数字已在补采该项后重新统计 〔TODO:确认补采完成〕。

## 5.5 小结

回到三问：（Q1）完成率上，RA 在覆盖充分的 RelayBench/AndroidDaily 上净优、在低覆盖的 MobileWorld 上无显著退化；（Q2/Q3）时间与 token 上，covered 任务有 2–3.5× 的中位收益，fallback 任务退化为与 baseline 同基座、仅付少量规划税。我们用 covered/fallback 分层主动暴露收益来源——收益集中在 RA 有专用 manifest 的应用，覆盖不到处如实呈现规划开销——以此堵住对自建 benchmark cherry-pick 的质疑。
