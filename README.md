# AppAgentCards

A community registry of **machine-readable cards** describing the AI agents embedded inside mobile apps — so that an OS-level agent (HarmonyOS Xiaoyi, Apple Intelligence, etc.) can hand off a user's request to the in-app agent that already knows the user's account, context, and data.

> **Status:** early draft. SPEC v0.1, seven verified Android reference manifests, MobileWorld adapter + multi-app flow runner (two reference flows). Contributors welcome.

---

## Why

Two existing paths for OS-level agents to act inside third-party apps each have problems:

- **A2A / App Intents / HMAF** — require vendor cooperation. Most super-apps do not cooperate.
- **Pure GUI agents** — drive the app's full UI. Brittle, slow, easy to detect, legally gray.

Most major mobile apps now ship their **own** in-app AI agent: Amap's voice tab, Yuanbao inside WeChat, Taobao's shopping assistant, Xiaohongshu's AI search, etc. These agents already have the user's login, address, payment, and preferences.

What's missing is a **discovery layer**: how does the OS-level agent know which apps have an embedded agent, where its input lives, what it can do, and when it must hand control back to the user?

This project is that layer. One card per app. GUI-mediated by default. Vendor-cooperation-optional.

## How a card is used

```
user: "Call an economy car to the airport"
        │
        ▼
OS-level agent
  1. receives an explicit target app package, e.g. com.autonavi.minimap
  2. matches the request against capabilities in that app's card
  3. picks `hail_ride`
  4. follows card.entry → opens Amap, taps the voice-input tab
  5. types the user's original prompt into the chat input
  6. honors `handoff_to_user_required: true` — returns control before payment
        │
        ▼
Amap AI assistant (the in-app agent) does the actual work
```

The target app is **explicit**. The OS agent selects a capability within that app. The in-app agent acts. The card is the contract.

## Demo

A real-device end-to-end case across two apps — one NL sentence, *"在上海找三家评价好的小众书店，挑一家打车过去"*, is routed to the `xhs_to_amap_place` flow: Xiaohongshu's 点点 returns three bookstores, the user picks one, and Amap's voice tab takes the pick straight into a ride-hailing card. The OS agent only steps in to relay the choice and to hand control back before the final CTA:

![Xiaohongshu → Amap bookstore + ride](assets/demo_xhs_to_amap_place.gif)

Driven by the [`xhs_to_amap_place` flow](manifests/_flows/xhs_to_amap_place.yaml), composing the [Xiaohongshu](manifests/com.xingin.xhs.yaml) and [Amap](manifests/com.autonavi.minimap.yaml) cards. Reproduce with:

```bash
uv run python scripts/run_nl.py "在上海找三家评价好的小众书店，挑一家打车过去" --record
```

## What's in the repo

```
AppAgentCards/
├── SPEC.md                    # manifest specification (v0.1)
├── SPEC-OPEN-QUESTIONS.md     # known design questions still in flight
├── spec/
│   └── schema.json            # JSON Schema mirror of SPEC (normative)
├── manifests/                 # one YAML card per app; seven Android cards + _flows/ for multi-app YAMLs
├── agents/                    # MobileWorld adapter, planner, capability router, card loader, flow runner, adb helper
├── scripts/                   # run_test.py (single app, cold-launches before mw test), run_flow.py (multi-app flows)
├── CONTRIBUTING.md
└── LICENSE                    # Apache-2.0
```

## Run under MobileWorld (multi-VLM real-device runner)

`agents/appcards_agent.py` plugs AppAgentCards into [MobileWorld](https://github.com/Tongyi-MAI/MobileWorld) as an `--agent-type`. MobileWorld gives us a real-device runner with provider-agnostic VLM support (Claude, Gemini, Qwen-VL, Kimi, …); the card supplies the deterministic entry path and handoff policy.

Requires **Python 3.12** (MobileWorld pins `>=3.12,<3.13`) and a Linux/WSL host with adb + a USB-debugging phone running `com.android.adbkeyboard/.AdbIME`.

```bash
# 1. set up the venv (project itself is not editable-installed; sync deps only)
uv venv --python 3.12
uv sync --no-install-project

# 2. install MobileWorld into the same venv
git clone https://github.com/Tongyi-MAI/MobileWorld && cd MobileWorld
uv pip install . --python /path/to/AppAgentCards/.venv/bin/python
# fastmcp 2.9.2 (a MobileWorld dep) breaks on pydantic ≥2.11 — pin it back
VIRTUAL_ENV=/path/to/AppAgentCards/.venv uv pip install "pydantic<2.11"
uv run mobile-world server &

# 3. fill in .env (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL) then drive a goal
cd /path/to/AppAgentCards
uv run python scripts/run_test.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
```

`scripts/run_test.py` loads `.env`, cold-launches the target app via `agents/_adb.py` (force-stop + monkey LAUNCHER), sets `APPCARDS_SKIP_OPEN_APP=1` so the planner skips its own `open_app` step, and forwards any extra flags (e.g. `--max-step 40`) straight through to `mw test`.

If you prefer to call `mw test` yourself, pass the LLM config explicitly:

```bash
set -a; source .env; set +a
export APPCARDS_TARGET_APP=com.aliyun.tongyi
uv run mw test "帮我点三杯蜜雪冰城蜜桃四季春" \
    --agent-type   "$PWD/agents/appcards_agent.py" \
    --model_name   "$LLM_MODEL" \
    --llm_base_url "$LLM_BASE_URL" \
    --api_key      "$LLM_API_KEY"
```

`--model_name` is provider-agnostic — point it at any OpenAI-compatible VLM (`qwen/qwen3-vl-235b-a22b`, `anthropic/claude-sonnet-4-5`, `google/gemini-3`, …). VLM token cost per task:

- 1 LLM call to pick a capability from the card.
- For each text selector, `uiautomator dump` is tried first (precise, free); a small VLM grounding call only on miss.
- `wait_for_reply` polls a VLM (`{done, text}`) on a wall-clock budget (`max(3×typical_latency, 30)` seconds) — this is usually the bulk of the VLM cost on chat-style capabilities.
- Coordinates from card `x_bounds` are used only as a last-resort fallback when the a11y tree doesn't expose the element.

Optional env vars (full list in `.env.example`):

- `APPCARDS_MANIFESTS=/path/to/manifests` — override the default `./manifests/` location.
- `APPCARDS_TARGET_DENSITY=480` — your phone's density in DPI for dp-aware `x_bounds` remapping. Without it the adapter falls back to raw bi-axial scaling.
- `APPCARDS_FRESH_CONV=0` — keep the previous conversation context across runs (default starts a fresh one).
- `APPCARDS_ANDROID_SERIAL=...` — pin every adb call to one device in multi-device setups.

The adapter honors `handoff_to_user_required`: for any irreversible capability it emits `ask_user` before the terminal CTA rather than auto-confirming.

### Multi-app flows

`scripts/run_flow.py` runs a YAML flow that chains multiple app cards — each step cold-launches one app via `agents/_adb.py`, pins a single capability, captures the in-app agent's reply, and feeds it forward to the next step via a small text-LLM extract call. Two reference flows live under `manifests/_flows/`:

```bash
# Xiaohongshu (POI discovery) → Amap (navigation)
uv run python scripts/run_flow.py manifests/_flows/xhs_to_amap_place.yaml \
    --input category="独立书店" --input city=北京

# Or use a natural-language request and let the LLM fill flow inputs:
uv run python scripts/run_flow.py manifests/_flows/xhs_to_amap_place.yaml \
    --nl "在北京找三家独立书店，挑一家打车过去"

# WeChat (chat summary) → WPS (doc generation)
uv run python scripts/run_flow.py manifests/_flows/wechat_to_wps_summary.yaml
```

### Natural-language entry point

`scripts/run_nl.py` takes a single NL sentence, builds a catalog of all app manifests + flows, and asks the text LLM to pick the best executor (single-app capability or multi-app flow) before dispatching. Use `--dry-run` to inspect the routing decision without launching anything.

```bash
uv run python scripts/run_nl.py "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_nl.py "在北京找三家独立书店，挑一家打车过去"
uv run python scripts/run_nl.py --dry-run "把和老王的聊天总结成一份周报 docx"
```

## Run tests

```bash
uv pip install .
python -m unittest discover -s tests -v              # device-less discovery; real-device tests skip without adb
```

Real-device tests require a connected Android device with target apps installed and `com.android.adbkeyboard/.AdbIME` enabled. Opt in via `tests/config_local.py` (gitignored):

```python
RUN_REAL_ADB_TESTS = True
```

```bash
python -m unittest tests.test_manifest_real_adb -v
```

Copy `.env.example` to `.env` and fill in your values (LLM endpoint is required; `ADB` path is optional). See `tests/config.py` for real-device test knobs (trajectory capture, result timeouts, screen recording). `test-results/` is gitignored — do not commit trajectories containing user data.

## MVP scope (v0.1)

Seven verified reference cards:

| App | Package | Capabilities |
| --- | --- | --- |
| Amap (高德地图) | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning |
| Tongyi Qwen (通义千问) | com.aliyun.tongyi | Chat, train/ride/food/hotel/movie booking |
| Ctrip (携程旅行) | ctrip.android.view | Flights, hotels, trains, attractions, package tours |
| Xiaohongshu (小红书) | com.xingin.xhs | Community UGC Q&A via AI search |
| Taobao (淘宝) | com.taobao.taobao | Product search, comparison, purchasing, order tracking |
| WeChat (微信) | com.tencent.mm | Yuanbao chat surface, AI search |
| WPS Office | cn.wps.moffice_eng | AI doc generation (PPT / Doc / writing assist) |

Quality bar per card: all required SPEC fields populated, ≥2 real example prompts per capability, verified manually within 30 days of submission, `handoff_to_user_required` correct for every irreversible capability.

## Known blockers

- **Taobao server-side risk control ("访问被拒绝").** Some Taobao capabilities — currently observed on `buy_product` and `order_local_delivery` — land on a server-rendered "亲，访问被拒绝" wall (an orange mascot + a feedback QR code) instead of the product / local-delivery flow. This is account- and device-level风控 enforced by Taobao, **not** an adapter or manifest bug: the entry path executes correctly, the AI shopping assistant accepts the prompt, and the failure happens on the deep-link target page after the in-app agent fires. There is no in-app workaround from the OS-agent side. Mitigations to try before declaring a capability broken: sign the device into a Taobao account with normal purchase history, complete any pending real-name / device-trust checks in 我的淘宝 → 设置 → 账号与安全, and avoid running the same risk-controlled capability back-to-back on a freshly-imaged device. We keep these capabilities in the manifest because the entry path itself is sound and the wall lifts once the account passes风控.

## What this project is *not*

- **Not a GUI agent.** We navigate to an in-app agent's input field, not the app's general UI. For general GUI agents, see AutoGLM / OMG-Agent.
- **Not a scraper.** Cards describe entry paths and capabilities, not data extraction. A conforming router does not read app data the user did not put there.
- **Not affiliated with any phone OEM or app vendor.** Neutral community spec. Vendors can publish official cards or not; the community can write one either way.
- **Not a challenger to A2A or MCP.** Forward-compatible by design (see SPEC §14). When apps ship A2A, cards become a thinner shim or disappear.

## Getting involved

- **Reading the spec:** start with [SPEC.md](SPEC.md), then [SPEC-OPEN-QUESTIONS.md](SPEC-OPEN-QUESTIONS.md).
- **Submitting a card:** see [CONTRIBUTING.md](CONTRIBUTING.md).
- **Code of conduct:** see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **Discussion:** GitHub Issues.

## License

Apache-2.0. See [LICENSE](LICENSE). Chosen for permissive enterprise use — the design only works if phone OEMs can adopt it without legal friction.
