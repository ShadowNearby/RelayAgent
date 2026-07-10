<h1 align="center">模拟器测试</h1>

<p align="center">
  <b>无真机路径：模拟器上能测什么、怎么搭</b>
</p>

<p align="center">
  <a href="emulator_testing.md">English</a> | <b>中文</b>
</p>

运行时是**纯 Python over adb**（`screencap` / `uiautomator dump` / `input` / `monkey`，见 `agents/runtime/native_runtime.py`），不依赖任何真机特有接口——Android 模拟器（AVD）天然兼容，且截图通常比真机的 ~1.5s/帧 快得多。本文给出没有真机时能测什么、怎么搭。

## 💻 1. 没有设备也能跑的部分（纯 LLM，零 adb）

规划侧完全不碰设备，装好 `.env` 就能跑：

```bash
uv run python scripts/run_plan.py --dry-run "帮我找一台适合学生的平板电脑，预算2000以内"   # NL flow 规划预览
uv run python scripts/run_benchmark_test.py --benchmark relaybench --plan-only            # plan-only 分层
uv run python -m unittest discover -s tests -v                                           # planner/runner 单元测试
```

## 📋 2. 模拟器上能测什么 / 不能测什么

| 层 | 模拟器可行性 |
| --- | --- |
| 运行时链路烟测（截图、uiautomator、AdbKeyboard 输入、手势、cold-launch）| ✅ 完全可行，`check_device_env.py` 全绿即可 |
| 国际 App 卡片（Gemini / Copilot / Reddit / Booking）| ✅ 需要 **Play Store 镜像** + 登录账号；Gemini 还需设备侧 Google 账号 |
| 中文垂类 App（千问 / 高德 / 携程 / 微信 / 小红书 / WPS）| ❌ **x86_64 上不可用**：ARM-only，转译下启动即崩（微信实测 SIGSEGV，见 §8.1）。**covered 层评测必须真机**（或 ARM64 主机 + arm64 镜像）|
| MobileWorld benchmark | ✅ 上游 MobileWorld 本来就是模拟器环境（自带 Mail/Mastodon/Files 等预置 App + 数据）；真机反而是我们的扩展（见 [`mobileworld_real_device.md`](mobileworld_real_device.md)）|

x86_64 镜像装 ARM-only 的国内 App 依赖 ARM 转译（API 30+ 自带），但 native 重的 App（微信等）转译下崩溃——见 §8.1 实测。Apple Silicon / ARM 主机用 arm64-v8a 镜像无此问题。

## 🛠️ 3. 搭一台 AVD

> **本仓库开发期使用的参考配置**：AVD `relay-test`（**android-36.0-Baklava（Android 16）/ google_apis_playstore / x86_64，pixel_9 档位 1080x2424**），KVM 加速，冷启动 ~15s，serial `emulator-5554`。选 **playstore 镜像**是为了能从 Play 商店官方装国际 App（见 §8）。`sdkmanager`/`avdmanager`/`emulator` 都需要 `JAVA_HOME`（snap Android Studio 的 JBR 即可：`export JAVA_HOME=/snap/android-studio/current/jbr`）。

```bash
# 1) 装 SDK 命令行工具后（JAVA_HOME 见上）：
sdkmanager "platform-tools" "emulator" "system-images;android-36.0-Baklava;google_apis_playstore;x86_64"

# 2) 建 AVD（pixel_9 档位 1080x2424）
echo no | avdmanager create avd -n relay-test \
  -k "system-images;android-36.0-Baklava;google_apis_playstore;x86_64" -d pixel_9 --force

# 3) 启动（评测用 -no-snapshot 保证冷状态；无显示器/服务器上加 -no-window）
~/Android/Sdk/emulator/emulator -avd relay-test \
  -no-snapshot -no-boot-anim -no-audio -no-window -gpu swiftshader_indirect &
adb wait-for-device
# 停止：adb -s emulator-5554 emu kill
```

> 镜像选型：**playstore** 镜像能官方装国际 App，但 system 分区只读、拿不到 `adb root`；要 `adb root`（侧载、改 system）就换 `google_apis`（非 playstore）镜像。两种都自带 `libndk_translation.so`（ARM 转译），但见 §8 的硬限制。

## 📱 4. 设备侧准备（与真机一致）

```bash
# AdbKeyboard（文本输入必须）
adb install ADBKeyBoard.apk        # github.com/senzhk/ADBKeyBoard

# 保持亮屏
adb shell settings put global stay_on_while_plugged_in 7

# 多设备时钉住模拟器
export RELAY_ANDROID_SERIAL=emulator-5554

# 体检
uv run python scripts/validate/check_device_env.py
```

`check_device_env.py` 会通过 `ro.kernel.qemu` / `ro.boot.qemu` 识别并标注模拟器，其余检查项与真机相同。

## 🔥 5. 烟测建议路径

1. `check_device_env.py` 全绿（IME / uiautomator / screencap）。
2. 装 + 登录一个国际 App（Copilot 最轻），跑单 App 入口：
   ```bash
   uv run python -m agents.runtime.native_runner com.microsoft.copilot "What is the tallest building in the world?"
   ```
   覆盖完整 obs→predict→execute 循环：cold-launch、入口 tap、AdbKeyboard 输入、`wait_for_reply` 文本-hash 判 done、scrape 回复。
3. （可选）NL flow 真跑：`uv run python scripts/run_plan.py --yes "Ask Copilot ..."`。

## 🖥️ 6. 用 scrcpy 观察/操控模拟器屏幕

模拟器以 `-no-window` headless 跑时，用 scrcpy 镜像屏幕（走设备端视频编码流，与有无原生窗口无关；对 agent 的 `screencap`/`uiautomator` 链路零影响，可一直开着旁观）。adb server（5037）与模拟器（5554/5555）都只监听 `127.0.0.1`，远程访问必须走 SSH 隧道。

**本机桌面**（Wayland 会话从非桌面 shell 拉起时要补会话变量；桌面终端里直接 `scrcpy -s emulator-5554` 即可）：

```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 SDL_VIDEODRIVER=wayland \
  scrcpy -s emulator-5554 --window-title "relay-test AVD"
```

**远程机器**（模拟器跑在服务器上时）——scrcpy 装在本地有屏幕的机器上，视频流经 SSH 隧道连到跑模拟器的服务器（下面记作 `user@emulator-host`）。

```bash
# 方案 B（首选，scrcpy 官方做法，单 adb server）：隧道服务器的 adb server + 视频端口
ssh -CN -L 15037:localhost:5037 -L 27183:localhost:27183 user@emulator-host  # 终端 1 挂着
export ADB_SERVER_SOCKET=tcp:127.0.0.1:15037           # 终端 2，本地 scrcpy ≥ 2.0
scrcpy -s emulator-5554 --force-adb-forward --tunnel-port=27183   # 序列号用服务器侧的 emulator-5554

# 方案 A（备选）：隧道模拟器 adbd 端口，本地 adb 直连
ssh -CN -L 15555:localhost:5555 user@emulator-host  # 终端 1
adb connect localhost:15555 && scrcpy -s localhost:15555               # 终端 2
```

> **首选方案 B。** 方案 A 常见报 `Device is unauthorized`——本地 adb 的密钥没被模拟器 adbd 信任，而 headless 模拟器没有授权弹窗可点，于是卡死。方案 B 让本地 scrcpy 复用**服务器侧那个已与模拟器握手过的 adb server**，根本不经过本地 adb 密钥认证，绕过此坑。真要用 A，得在服务器侧把本地公钥灌进模拟器（仅 `adb root` 镜像可行）或关 `ro.adb.secure`，不建议。

## 📦 7. 在模拟器上跑端侧 APK（android/ App）

debug 构建的 `abiFilters` 含 `x86_64`（Chaquopy 只认 `defaultConfig` 里的 abiFilters，见 `android/app/build.gradle.kts`），所以同一个 APK 真机/模拟器都能装：

```bash
cd android && JAVA_HOME=/snap/android-studio/current/jbr ANDROID_HOME=~/Android/Sdk ./gradlew :app:assembleDebug
adb install -r -t app/build/intermediates/apk/debug/app-debug.apk   # intermediates 产物带 testOnly 标记，要 -t

# 免手点开无障碍（真机上走系统设置 UI）
adb shell settings put secure enabled_accessibility_services \
  com.relayagent.app/com.relayagent.app.RelayAccessibilityService
adb shell settings put secure accessibility_enabled 1
```

LLM 网关在 App 设置页填（同 `.env` 三项）；MediaProjection 授权弹窗每次运行都要点一次「Start now」（Android 14 起 per-session）。已在 relay-test AVD 上验证：CPython 启动、`OnDeviceAndroidBackend` 注入、MediaProjection 截帧（喂 grounding VLM）、三段式路由 + flow 规划 + in-process leg 执行、traj/wall_clock 落 filesDir。**垂类 App 没装时 leg 在 cold-launch / grounding 处明确失败**——端到端成功仍需装好并登录目标 App（§2 的限制）。

## 🧪 8. 装 manifest 里的垂类 App（实测结论）

manifest 共 10 个 App，按来源分两类，**结论：x86_64 模拟器只适合装国际 App，国内 ARM-only App 装得上但跑不起来。**

### 8.1 国内 6 个（微信/通义/高德/携程/WPS/小红书）——x86_64 上不可用 ❌

这些是 **ARM-only**（厂商不发 x86 包）。即使镜像带 `libndk_translation.so`（ARM→x86 转译），**native 重的 App 启动即崩**：

- **微信实测**（官方腾讯 CDN `dldir1v6.qq.com/weixin/android/...arm64.apk`，250MB）：`adb install` 成功、桌面有图标，但一启动确定性崩溃——`Fatal signal 11 (SIGSEGV)` in `wc_srvinit_1`，重试同样无进程。
- 推论：其余 5 个国内 App 同理高风险。**covered 层正式评测必须用真 arm64 设备**（与 §2 一致）；要在模拟器跑国内 App，得在 **Apple Silicon / ARM64 主机**上用 arm64-v8a 镜像（无需转译）。
- 国内 App 官网多为 JS 下载页、无直链；除微信（官方 CDN 直链）外，其余只能第三方镜像——**非官方源不可信，不要随意拉**。

### 8.2 国际 4 个（Gemini `com.google.android.apps.bard` / Copilot / Reddit / Booking）——走 Play 商店 ✅

playstore 镜像 + **用户自己登录 Google 账号**后从 Play 商店装（x86 split 由商店下发，原生跑，不依赖转译）。

- 登录是人工步骤：`adb` 无法输入凭据，须经 scrcpy（§6）人工点 **Sign in**、输入账号密码并接受 Play 条款。
- 落地登录页：`adb shell monkey -p com.android.vending -c android.intent.category.LAUNCHER 1`，停在 `UnauthenticatedMainActivity` 的 Sign in。
- 登录后再装 App：可 `adb shell am start -a android.intent.action.VIEW -d 'market://details?id=<pkg>'` 跳详情页人工点 Install，或在商店内搜。

## ⚠️ 9. 已知差异（模拟器 vs 真机）

- 截图快（典型 <0.5s/帧）→ 墙钟数字**不可与真机混在同一张表**；评测结论仍以真机为准。
- 无蜂窝/短信/NFC；定位是模拟值（高德「附近」类任务结果不真实）。
- 部分 App 检测模拟器直接拒跑或降级（国内 App 风控常见）。
- **ARM-only App 在 x86_64 上崩溃**（§8.1）——这是平台级硬限制，非配置问题。
