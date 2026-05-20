#!/usr/bin/env python3
"""
examples/match_intent.py — minimal AppAgentCards app-scoped router demo.

Given an explicit app and a user prompt, this script:

  1. Loads every YAML manifest under ../manifests/.
  2. Selects the requested app by app_id.
  3. Scores that app's capabilities against the prompt with a naive
     keyword-overlap heuristic over `description` and `example_prompts`.
     The prompt no longer routes across apps; the caller must specify the app.
  4. Prints the top-3 capability candidates and a routing plan for the best match:
     entry steps, preconditions, invocation, x_bounds remap (if the
     target device differs from `provenance.x_device_metrics`),
     handoff policy, output handling.

Nothing is actually tapped or executed. The point is to show what a
router would do, on what coordinates, before any UI side effect.

Install:
    uv venv && source .venv/bin/activate
    uv pip install pyyaml

Usage:
    python match_intent.py --app com.autonavi.minimap "导航到上海外滩"
    python match_intent.py --app ctrip.android.view "上海外滩附近800元以内的酒店"
    python match_intent.py --app com.xingin.xhs "周末上海带娃去哪玩" \\
        --device-resolution 1440x3120 --device-density 480
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "manifests"


# ---------- Intent matching (deliberately naive) ----------

def tokenize(text: str) -> set[str]:
    """Coarse bilingual tokenization: latin words AND single CJK chars,
    so even short Chinese prompts produce useful overlap signal."""
    text = text.lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    chars = {c for c in text if "一" <= c <= "鿿"}
    return words | chars


def score(prompt_tokens: set[str], capability: dict) -> int:
    """Sum of token overlap, with example_prompts weighted 3x over description.
    Real routers replace this with an LLM-judged match — but for a deterministic
    demo this is enough to differentiate "打车" from "订酒店"."""
    s = 0
    for ex in capability["example_prompts"]:
        s += 3 * len(prompt_tokens & tokenize(ex))
    s += len(prompt_tokens & tokenize(capability["description"]))
    return s


# ---------- x_bounds remap ----------

def _px_to_dp(px: int, dpi: int) -> float:
    return px * 160.0 / dpi


def _dp_to_px(dp: float, dpi: int) -> int:
    return int(round(dp * dpi / 160.0))


def remap_bounds(bounds: dict, src_metrics: dict,
                 tgt_resolution: tuple[int, int], tgt_density: int) -> list[int]:
    """Remap an x_bounds box from the verified device to a target device.

    Strategy is anchor-aware:
      - bottom_right / top_right / bottom_left / top_left:
        preserve dp margins from the named edges (handles Android's
        dp-anchored UIs across different densities and aspect ratios).
      - center / none / unknown:
        fall back to bi-axial linear pixel scaling (lossy when aspect
        ratios differ — but honestly the best we can do without semantics).
    """
    x1, y1, x2, y2 = bounds["box"]
    anchor = bounds.get("anchor", "none")
    src_w, src_h = src_metrics["resolution_px"]
    src_dpi = src_metrics["density_dpi"]
    tgt_w, tgt_h = tgt_resolution

    w_dp = _px_to_dp(x2 - x1, src_dpi)
    h_dp = _px_to_dp(y2 - y1, src_dpi)
    w_px = _dp_to_px(w_dp, tgt_density)
    h_px = _dp_to_px(h_dp, tgt_density)

    def edge_anchor(left_margin_px: int | None, top_margin_px: int | None,
                    right_margin_px: int | None, bottom_margin_px: int | None) -> list[int]:
        if left_margin_px is not None:
            x1_t = _dp_to_px(_px_to_dp(left_margin_px, src_dpi), tgt_density)
            x2_t = x1_t + w_px
        else:
            x2_t = tgt_w - _dp_to_px(_px_to_dp(right_margin_px, src_dpi), tgt_density)
            x1_t = x2_t - w_px
        if top_margin_px is not None:
            y1_t = _dp_to_px(_px_to_dp(top_margin_px, src_dpi), tgt_density)
            y2_t = y1_t + h_px
        else:
            y2_t = tgt_h - _dp_to_px(_px_to_dp(bottom_margin_px, src_dpi), tgt_density)
            y1_t = y2_t - h_px
        return [x1_t, y1_t, x2_t, y2_t]

    if anchor == "bottom_right":
        return edge_anchor(None, None, src_w - x2, src_h - y2)
    if anchor == "top_right":
        return edge_anchor(None, y1, src_w - x2, None)
    if anchor == "bottom_left":
        return edge_anchor(x1, None, None, src_h - y2)
    if anchor == "top_left":
        return edge_anchor(x1, y1, None, None)

    # center / none / unknown → bi-axial linear scale
    sx = tgt_w / src_w
    sy = tgt_h / src_h
    return [int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)]


# ---------- Plan rendering ----------

_FIRST_CLASS = ("accessibility_id", "resource_id", "text", "text_contains", "xpath")


def render_selector(sel: dict) -> str:
    for k in _FIRST_CLASS:
        if k in sel:
            return f"{k}={sel[k]!r}"
    if "x_bounds" in sel:
        b = sel["x_bounds"]
        return f"x_bounds(box={b['box']}, anchor={b.get('anchor', 'none')})"
    return repr(sel)


def maybe_remap(sel: dict, card: dict,
                tgt_res: tuple[int, int], tgt_density: int) -> str | None:
    """If selector uses x_bounds and target device differs, return a
    one-line description of the remap. Otherwise None."""
    if "x_bounds" not in sel:
        return None
    src = card["provenance"].get("x_device_metrics")
    if not src:
        return "  ⚠️  x_bounds used but provenance.x_device_metrics missing — router cannot remap safely"
    if tuple(src["resolution_px"]) == tgt_res and src["density_dpi"] == tgt_density:
        return None
    remapped = remap_bounds(sel["x_bounds"], src, tgt_res, tgt_density)
    return (f"  ↳ remap {src['resolution_px']}@{src['density_dpi']}dpi → "
            f"{list(tgt_res)}@{tgt_density}dpi: box={remapped}")


def render_step(step: dict, card: dict,
                tgt_res: tuple[int, int], tgt_density: int) -> str:
    if "tap" in step:
        out = f"  tap   {render_selector(step['tap'])}"
        rm = maybe_remap(step["tap"], card, tgt_res, tgt_density)
        return out + ("\n" + rm if rm else "")
    if "wait" in step:
        return f"  wait  {step['wait']}"
    if "swipe" in step:
        return f"  swipe {step['swipe']}"
    return f"  ???   {step!r}"


def select_card(cards: list[dict], app: str) -> dict:
    """Select a card by package/bundle app_id."""
    exact = [card for card in cards if app == card["app_id"]]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        sys.exit(f"Ambiguous app {app!r}; matched multiple cards.")

    app_ids = ", ".join(
        f"{card['app_id']} ({card['app_name']})" for card in cards
    )
    sys.exit(f"Unknown app_id {app!r}. Available apps: {app_ids}")


def plan(prompt: str, card: dict, capability: dict,
         tgt_res: tuple[int, int], tgt_density: int) -> None:
    ea = card["embedded_agent"]
    print(f"\n========== ROUTING PLAN ==========")
    print(f"User prompt    : {prompt}")
    print(f"Selected card  : {card['app_name']} ({card['app_id']}) v{card['card_version']}")
    print(f"Embedded agent : {ea['name']} [{ea['type']}]")
    print(f"Capability     : {capability['id']}")
    print(f"  executable               : {capability['executable']}")
    print(f"  side_effects             : {capability['side_effects'] or '[]'}")
    print(f"  reversible               : {capability['reversible']}")
    print(f"  requires_login           : {capability['requires_login']}")
    print(f"  handoff_to_user_required : {capability['handoff_to_user_required']}")
    print(f"  typical_latency_seconds  : {capability.get('typical_latency_seconds', '?')}")

    if not capability["executable"]:
        print("  ℹ️  agent only informs/recommends; do not promise the user "
              "the action will be completed.")
    if capability["handoff_to_user_required"]:
        print("  ⚠️  HANDOFF: router MUST return control before the terminal "
              "action; do NOT auto-tap CTAs, do NOT auto-confirm.")

    preconds = ea["entry"].get("preconditions") or []
    if preconds:
        print(f"\n--- Preconditions (verify before entering) ---")
        for p in preconds:
            print(f"  - {p}")

    print(f"\n--- Entry (method={ea['entry']['primary']['method']}) ---")
    steps = ea["entry"]["primary"].get("steps", [])
    if not steps:
        print("  (no taps required — app cold-launch lands on agent surface)")
    for st in steps:
        print(render_step(st, card, tgt_res, tgt_density))

    print(f"\n--- Invocation ---")
    field = ea["invocation"]["input"]["field"]
    print(f"  focus input  {render_selector(field)}")
    rm = maybe_remap(field, card, tgt_res, tgt_density)
    if rm:
        print(rm)

    tpl = ea["invocation"].get("prompt_template", "{{user_prompt}}")
    rendered = tpl.replace("{{user_prompt}}", prompt).replace(
        "{{capability_id}}", capability["id"])
    print(f"  type text    {rendered!r}")

    submit = ea["invocation"]["submit"]["trigger"]
    print(f"  tap submit   {render_selector(submit)}")
    rm = maybe_remap(submit, card, tgt_res, tgt_density)
    if rm:
        print(rm)

    out_method = (ea.get("output") or {}).get("method", "none")
    print(f"\n--- After submit ---")
    if out_method == "none":
        print("  output.method=none → hand control back to user; "
              "do not poll or read the response. User completes any "
              "in-app card / CTA / partner auth themselves.")
    else:
        print(f"  output.method={out_method} → router may attempt to "
              "extract the response per output.completion_signal.")
    print(f"==================================\n")


# ---------- Main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app", required=True,
                    help="Required package/bundle app_id; app selection is explicit.")
    ap.add_argument("prompt", help="The user's natural-language request.")
    ap.add_argument("--device-resolution", default="1080x2424",
                    help="Target device WxH in px (default: Pixel 9 = 1080x2424)")
    ap.add_argument("--device-density", type=int, default=420,
                    help="Target device density in DPI (default: Pixel 9 = 420)")
    ap.add_argument("--top", type=int, default=3,
                    help="How many top candidates to display (default 3)")
    args = ap.parse_args()

    try:
        w, h = (int(x) for x in args.device_resolution.lower().split("x"))
    except ValueError:
        sys.exit("--device-resolution must look like 1080x2424")
    tgt_res = (w, h)

    cards = []
    for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            cards.append(yaml.safe_load(f))
    if not cards:
        sys.exit(f"No manifests found under {MANIFESTS_DIR}")

    card = select_card(cards, args.app)

    pt = tokenize(args.prompt)
    scored = []
    for cap in card["embedded_agent"]["capabilities"]:
        s = score(pt, cap)
        if s > 0:
            scored.append((s, cap))
    if not scored:
        sys.exit(f"No capability in {card['app_id']} matched the prompt "
                 "(zero token overlap).")
    scored.sort(key=lambda t: -t[0])

    print(f"Selected app: {card['app_id']} ({card['app_name']})")
    print(f"Top {min(args.top, len(scored))} capability matches:")
    for s, cap in scored[:args.top]:
        print(f"  [{s:3d}] {cap['id']}")

    _, top_cap = scored[0]
    plan(args.prompt, card, top_cap, tgt_res, args.device_density)


if __name__ == "__main__":
    main()
