# 用 MobileWorld 跑真机测试

记录如何用 **MobileWorld**（独立仓库，本机在 `/home/yjs/MobileWorld`）通过 ADB 驱动真机，
以 **SJTU IPADS 网关的 qwen** 作为 agent 大脑，跑一个临时目标（如让 Google Maps 导航）。

> MobileWorld 已从 RelayAgent 主仓库移除（见项目记忆「Dropped MobileWorld」），
> 它是一个 Docker 模拟器上的 benchmark（201 个预定义任务 + 评测器）。
> 但它的 **real-device 模式** 可以直接用 ADB 驱动物理机跑**任意自然语言目标**，
> 不需要预先写 task 类——这正是录对比视频要用的入口。

## 前置条件

- 物理 Android 机 USB 连接，已开 USB 调试（`adb devices` 能看到 `device`）。
- 已装 ADB platform-tools。
- AdbKeyboard 用于文本输入（MobileWorld 会自动装；手动：
  `adb install /home/yjs/MobileWorld/ADBKeyboard.apk` 后
  `adb shell ime enable com.android.adbkeyboard/.AdbIME`）。
- 目标 app 已装在机上（本机 Pixel 9 已装 `com.google.android.apps.maps`）。
- 多设备时用 `RELAY_ANDROID_SERIAL` / `ANDROID_SERIAL` 选设备。

## 凭证（SJTU 网关 qwen）

复用 RelayAgent 的 `.env`（**别提交、别复述完整 key**）：

| 参数 | 值 |
| --- | --- |
| `--llm_base_url` | `http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1` |
| `--model_name` | `qwen` |
| `--api_key` | `.env` 里的 `LLM_API_KEY` |
| `--agent-type` | `general_e2e`（qwen-3.5 适用，相对坐标 0–1000） |

## 步骤

```bash
cd /home/yjs/MobileWorld

# 1. 装依赖（首次）
uv sync

# 2. 起后端 server（默认 0.0.0.0:6800，桥接 model ↔ 设备）
#    若已在跑会报 "address already in use"，直接复用即可，不用重起。
uv run mw server          # 等价 uv run mobile-world server

# 2b. 确认 server 在线且认到设备（ok:true + 设备序列号）
curl -s http://127.0.0.1:6800/health
#   {"ok":true,"devices":["46180DLAQ004LW"],"device_status":{...:true}}

# 3. 另开一个终端，跑临时目标（英文 goal 直接传）
#    LLM_API_KEY 从 RelayAgent 的 .env 取，别硬编码进命令历史
uv run mw test "Live navigate to the Bund by Google Map" \
    --agent-type general_e2e \
    --model-name qwen \
    --llm-base-url http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1 \
    --aw-host http://127.0.0.1:6800 \
    --api-key "$LLM_API_KEY" \
    --max-round 25 --timeout 600
```

> 旗标连字符/下划线两种写法都收（`--model-name` == `--model_name`）。
> `mw test` 是临时单任务入口，**不做 task 初始化/校验**，直接对当前设备屏幕开跑。
> `--max-round` 限步数（默认 -1 不限），`--timeout` 限墙钟秒数。

把 key 喂进去而不落进 shell history 的写法：

```bash
export LLM_API_KEY=$(grep '^LLM_API_KEY=' /home/yjs/RelayAgent/.env | cut -d= -f2-)
uv run mw test "..." ... --api_key "$LLM_API_KEY"
```

## ⚠️ 关键：先手动拉起目标 app，别让 agent 从桌面找

从**桌面**起跑时，qwen 在 Pixel 9 上会反复 `scroll up` 想开应用抽屉，但这台机从桌面上滑会
拉出**通知栏**，agent 看到通知栏又想"上滑关掉"，就此死循环（实测白烧到 step 9 仍没进 app）。

**解法：跑 `mw test` 前先 `monkey` 把目标 app 拉到前台**，agent 从 app 内开始就稳了
（外滩那次预开 Maps 后 5 步搞定）：

```bash
adb shell am force-stop com.google.android.apps.maps
adb shell monkey -p com.google.android.apps.maps -c android.intent.category.LAUNCHER 1
sleep 6
adb shell dumpsys window | grep -m1 mCurrentFocus   # 确认 MapsActivity 在前台
```

goal 仍写完整意图（"Live navigate to the Bund by Google Map"），agent 会在 app 内
搜索 + 起导航，不受预开影响。

## 实时看屏 / 录屏

- 看实时设备画面：`uv run mw device`。
- 录屏：`adb screenrecord` 单段上限 180s，导航演示常会超，用**分段循环录 + ffmpeg 合并**。
  下面这个脚本（本机存在 `/tmp/mw_rec.sh`）跑前后台起、靠哨兵文件 `/tmp/mw_rec.stop` 收尾：

```bash
#!/usr/bin/env bash
# 分段录屏：循环 180s screenrecord，出现 /tmp/mw_rec.stop 后停止并 ffmpeg 合并
set -u
OUTDIR="$1"; SERIAL="${RELAY_ANDROID_SERIAL:-46180DLAQ004LW}"; ADB="adb -s $SERIAL"
mkdir -p "$OUTDIR"; rm -f /tmp/mw_rec.stop; i=0; LIST="$OUTDIR/concat.txt"; : > "$LIST"
while [ ! -f /tmp/mw_rec.stop ]; do
  dev="/sdcard/mwrec_$(printf %03d $i).mp4"
  $ADB shell screenrecord --time-limit 180 "$dev"
  loc="$OUTDIR/chunk_$(printf %03d $i).mp4"
  $ADB pull "$dev" "$loc" >/dev/null 2>&1 && echo "file '$(basename "$loc")'" >> "$LIST"
  $ADB shell rm -f "$dev" >/dev/null 2>&1; i=$((i+1))
done
[ -s "$LIST" ] && ffmpeg -y -f concat -safe 0 -i "$LIST" -c copy "$OUTDIR/recording.mp4" \
  >/dev/null 2>&1 && echo "SAVED $OUTDIR/recording.mp4"
```

用法（起→跑→收）：

```bash
OUT=/home/yjs/RelayAgent/recordings/mw_bund_$(date +%Y%m%d_%H%M%S)
/tmp/mw_rec.sh "$OUT" > /tmp/mw_rec.log 2>&1 &     # 起录屏
# ... 预开 app + 跑 mw test ...
touch /tmp/mw_rec.stop                              # 通知收尾
adb shell pkill -2 screenrecord                     # 结束当前分段，让循环看到哨兵退出
# 合并后 recording.mp4 落在 $OUT/，可删 chunk_*.mp4 与 concat.txt
```

> 收尾要点：只 `touch` 哨兵不够——当前 `screenrecord` 还在阻塞那 180s，必须 `pkill` 掉
> 设备端 screenrecord 才会结束本段、循环才看到哨兵退出并合并。

## 实测示例（2026-06-07 已跑通）

| 项 | 值 |
| --- | --- |
| goal | `Live navigate to the Bund by Google Map` |
| 驱动 | SJTU 网关 qwen，`general_e2e` |
| 起点 | 预开 Google Maps（见上「关键」） |
| 结果 | **5 步**：点搜索框 → 输入 "The Bund" → 选中外滩(Zhongshan Rd E-1, Waitan, Huangpu) → Start → 进入实时逐向导航（35 km / 42 min / 蓝色路线）|
| 录屏 | `recordings/mw_bund_<ts>/recording.mp4`（1080×2424，约 86s）|

对照：**不预开、从桌面起**那次，agent 卡在 `scroll up` 死循环烧到 step 9 没进 app —— 故必须预开。

## 模型 / 坐标系参考

`docs/real-devices.md`（MobileWorld 仓库内）的对照表：

| 模型 | agent-type | 坐标系 |
| --- | --- | --- |
| qwen-3.5 | `general_e2e` | 相对 0–1000 |
| Gemini 3 Pro | `general_e2e` | 相对 0–1000 |
| Claude Opus/Sonnet | `general_e2e` | 绝对像素（Sonnet 需 resize 到 1280×720） |
| Seed-2.0-Pro | `seed_agent` | 相对 0–1000 |

## 注意

- 这是真机直驱，会真实改设备状态（起导航、定位等）。导航类目标依赖设备真实 GPS / 网络；
  在国内需保证 Google 服务可达。
- 模拟器模式（`uv run mobile-world ...` 跑 Docker 快照）可能没有真实 GPS / 实时导航，
  录导航演示请用 real-device 模式。
- 命令里 **绝不** 硬编码 API key；用 `$LLM_API_KEY` 从 `.env` 注入。
