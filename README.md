# AppAgentCards

A community registry of **machine-readable cards** describing the AI agents embedded inside mobile apps — so that an OS-level agent (HarmonyOS Xiaoyi, Apple Intelligence, etc.) can hand off a user's request to the in-app agent that already knows the user's account, context, and data.

> **Status:** early draft. SPEC v0.1, five verified Android reference manifests, demo router included. Contributors welcome.

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

A real-device end-to-end case — the OS agent routes `order_food` to Tongyi Qianwen, the in-app agent handles the interaction, and the OS agent steps in only for the post-result confirmation flow (pick item → pick spec → stop before payment):

![Order via Tongyi Qianwen](assets/demo-mixue-order.gif)

Driven by the [Tongyi Qianwen manifest](manifests/com.aliyun.tongyi.yaml). The card's `order_food` capability defines the entry, invocation prompt, and the `x_post_result_flow` that taps through the confirmation UI.

## What's in the repo

```
AppAgentCards/
├── SPEC.md                    # manifest specification (v0.1)
├── SPEC-OPEN-QUESTIONS.md     # known design questions still in flight
├── spec/
│   └── schema.json            # JSON Schema mirror of SPEC (normative)
├── manifests/                 # one YAML card per app; five Android cards
├── examples/
│   └── match_intent.py        # demo router: capability matching + plan output
├── CONTRIBUTING.md
└── LICENSE                    # Apache-2.0
```

## Try the demo router

The demo router requires an explicit package id via `--app`, scores that app's capability descriptions with a keyword-overlap heuristic, then prints the entry, invocation, `x_bounds` remapping, handoff, and output plan. It does not tap or operate any app UI.

```bash
uv venv && source .venv/bin/activate && uv pip install .

python examples/match_intent.py --app com.autonavi.minimap "Navigate to the Bund in Shanghai"
python examples/match_intent.py --app ctrip.android.view "Hotel near the Bund under 800 yuan"
python examples/match_intent.py --app com.taobao.taobao "Find a tablet for students under 2000 yuan"
python examples/match_intent.py --app com.xingin.xhs "Weekend activities with kids in Shanghai" \
  --device-resolution 1440x3120 --device-density 480
```

## Run tests

```bash
uv pip install .
python -m unittest discover -s tests -v              # unit tests (no device needed)
python -m unittest tests.test_manifest_cli -v        # CLI smoke tests only
```

Real-device tests require a connected Android device with target apps installed and `com.android.adbkeyboard/.AdbIME` enabled. Configure via `tests/config_local.py` (gitignored):

```python
RUN_REAL_ADB_TESTS = True
```

```bash
python -m unittest tests.test_manifest_real_adb -v
```

Copy `.env.example` to `.env` and fill in your values for optional features (vision API, custom adb path). See `tests/config.py` for all settings (vision summary, trajectory capture, result timeouts). `test-results/` is gitignored — do not commit trajectories containing user data.

## MVP scope (v0.1)

Five verified reference cards:

| App | Package | Capabilities |
| --- | --- | --- |
| Amap | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning |
| Tongyi Qwen | com.aliyun.tongyi | Chat, train/ride/food/hotel/movie booking |
| Ctrip | ctrip.android.view | Flights, hotels, trains, attractions, package tours |
| Xiaohongshu | com.xingin.xhs | Community UGC Q&A via AI search |
| Taobao | com.taobao.taobao | Product search, comparison, purchasing, order tracking |

Quality bar per card: all required SPEC fields populated, ≥2 real example prompts per capability, verified manually within 30 days of submission, `handoff_to_user_required` correct for every irreversible capability.

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
