# 模拟器测试（无真机路径）

> English: [`emulator_testing.md`](emulator_testing.md)

运行时是**纯 Python over adb**（`screencap` / `uiautomator dump` / `input` / `monkey`，见 `agents/native_runtime.py`），不依赖任何真机特有接口——Android 模拟器（AVD）天然兼容，且截图通常比真机的 ~1.5s/帧 快得多。本文给出没有真机时能测什么、怎么搭。

## 1. 没有设备也能跑的部分（纯 LLM，零 adb）

规划侧完全不碰设备，装好 `.env` 就能跑：

```bash
uv run python scripts/run_plan.py --dry-run "帮我找一台适合学生的平板电脑，预算2000以内"   # NL flow 规划预览
uv run python scripts/run_benchmark_test.py --benchmark relaybench --plan-only            # plan-only 分层
uv run python -m unittest discover -s tests -v                                           # planner/runner 单元测试
```

## 2. 模拟器上能测什么 / 不能测什么

| 层 | 模拟器可行性 |
| --- | --- |
| 运行时链路烟测（截图、uiautomator、AdbKeyboard 输入、手势、cold-launch）| ✅ 完全可行，`check_device_env.py` 全绿即可 |
| 国际 App 卡片（Gemini / Copilot / Reddit / Booking）| ✅ 需要 **Play Store 镜像** + 登录账号；Gemini 还需设备侧 Google 账号 |
| 中文垂类 App（千问 / 高德 / 携程 / 微信 / 小红书 / WPS）| ⚠️ APK 可侧载，但登录要 SMS 验证、账号风控对模拟器明显更严（千问购物/高德打车基本不可用）；**covered 层正式评测仍需真机** |
| MobileWorld benchmark | ✅ 上游 MobileWorld 本来就是模拟器环境（自带 Mail/Mastodon/Files 等预置 App + 数据）；真机反而是我们的扩展（见 [`mobileworld_real_device.md`](mobileworld_real_device.md)）|

x86_64 镜像装 ARM-only 的国内 App 依赖 ARM 转译（API 30+ 自带，慢且偶有崩溃）；Apple Silicon / ARM 主机用 arm64-v8a 镜像无此问题。

## 3. 搭一台 AVD

```bash
# 1) 装 SDK 命令行工具后：
sdkmanager "platform-tools" "emulator" "system-images;android-35;google_apis_playstore;x86_64"

# 2) 建 AVD（Pixel 9 档位接近我们真机参数 1080x2400）
avdmanager create avd -n relay-test -k "system-images;android-35;google_apis_playstore;x86_64" -d pixel_8

# 3) 启动（评测用 -no-snapshot 保证冷状态）
emulator -avd relay-test -no-snapshot -no-boot-anim &
adb wait-for-device
```

> 需要侧载国内 App 时换 `google_apis`（非 playstore）镜像可拿 `adb root`；只测国际 App 用 playstore 镜像。

## 4. 设备侧准备（与真机一致）

```bash
# AdbKeyboard（文本输入必须）
adb install ADBKeyBoard.apk        # github.com/senzhk/ADBKeyBoard

# 保持亮屏
adb shell settings put global stay_on_while_plugged_in 7

# 多设备时钉住模拟器
export RELAY_ANDROID_SERIAL=emulator-5554

# 体检
uv run python scripts/check_device_env.py
```

`check_device_env.py` 会通过 `ro.kernel.qemu` / `ro.boot.qemu` 识别并标注模拟器，其余检查项与真机相同。

## 5. 烟测建议路径

1. `check_device_env.py` 全绿（IME / uiautomator / screencap）。
2. 装 + 登录一个国际 App（Copilot 最轻），跑单 App 入口：
   ```bash
   uv run python -m agents.native_runner com.microsoft.copilot "What is the tallest building in the world?"
   ```
   覆盖完整 obs→predict→execute 循环：cold-launch、入口 tap、AdbKeyboard 输入、`wait_for_reply` 文本-hash 判 done、scrape 回复。
3. （可选）NL flow 真跑：`uv run python scripts/run_plan.py --yes "Ask Copilot ..."`。

## 6. 已知差异（模拟器 vs 真机）

- 截图快（典型 <0.5s/帧）→ 墙钟数字**不可与真机混在同一张表**；评测结论仍以真机为准。
- 无蜂窝/短信/NFC；定位是模拟值（高德「附近」类任务结果不真实）。
- 部分 App 检测模拟器直接拒跑或降级（国内 App 风控常见）。
