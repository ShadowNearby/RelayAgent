"""MobileWorld adapter for AppAgentCards.

Run with:

    APPCARDS_TARGET_APP=com.aliyun.tongyi \\
    mw test "在通义里点一杯蜜雪冰城" \\
        --agent-type /abs/path/AppAgentCards/agents/appcards_agent.py \\
        --model_name anthropic/claude-sonnet-4-5

Design:
- Subclass MobileWorld's MCPAgent so we get its provider-agnostic openai
  client, token accounting, and the model_name plumbing for free.
- One LLM call per task picks a capability + invocation text from the card.
- The rest of the turns walk a deterministic plan: open_app, taps using
  card x_bounds, input_text, submit, optional post-result flow.
- Text-based selectors (input field focus, post-result labels) try
  `uiautomator dump` first (precise, free, robust to redraws); only fall
  back to a small VLM grounding call if the text is not in the a11y tree.
- `wait_for_reply` polls a VLM (`{done, text}`) on a WALL-CLOCK budget
  (`max(3×typical_latency, 30)` seconds), not a poll-count budget.
- Honors `handoff_to_user_required`: emits ask_user before the irreversible CTA.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# MobileWorld loads this file via importlib.util.spec_from_file_location, so
# the package directory is NOT on sys.path automatically. Add the repo root
# so the sibling modules under `agents/` resolve as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from loguru import logger

from mobile_world.agents.base import MCPAgent
from mobile_world.agents.utils.helpers import pil_to_base64
from mobile_world.runtime.utils.models import JSONAction

from agents._adb import adb_base, force_stop, swipe_down
from agents.action_planner import Step, build_plan
from agents.capability_router import route_capability
from agents.card_loader import bounds_center, load_card_by_app_id

_TARGET_APP_ENV = "APPCARDS_TARGET_APP"
_MANIFESTS_ENV = "APPCARDS_MANIFESTS"
_DENSITY_ENV = "APPCARDS_TARGET_DENSITY"
_FRESH_CONV_ENV = "APPCARDS_FRESH_CONV"  # set to "0" to disable
_SKIP_OPEN_APP_ENV = "APPCARDS_SKIP_OPEN_APP"  # set to "1" if caller pre-launched the app
_REPLY_OUT_ENV = "APPCARDS_REPLY_OUT"  # path; if set, captured reply is dumped as JSON at handoff/done
_DISMISS_PERMS_ENV = "APPCARDS_DISMISS_PERMISSIONS"  # set to "0" to disable system permission popup auto-dismiss

# MobileWorld writes the active run under traj_logs/user_task/ (see CLAUDE.md).
# We append every LLM call into traj.json at top-level under "0".llm_calls so
# the calls live alongside the per-step traj entries. MW's log_traj rewrites
# the whole file each step but preserves unknown sibling keys, so the field
# survives across step writes.
_TRAJ_DIR = Path("traj_logs") / "user_task"

_GROUNDING_SYSTEM = (
    "You are a UI grounding model. Given a phone screenshot and a target "
    "element description, return the click point as JSON with normalized "
    "coordinates in [0, 999]. Reply with ONE ```json``` fenced object: "
    '{"x": <int 0-999>, "y": <int 0-999>}. Pick the visible center of the '
    "element. If you cannot find it, reply with "
    '{"x": null, "y": null}.'
)
_REPLY_WATCH_SYSTEM = (
    "You watch an in-app AI assistant render its reply on a phone screen. "
    "Decide whether the assistant has FINISHED responding to the user's most "
    "recent message. Signals that it is still generating: a streaming "
    "cursor, a 'Stop'/'停止生成'/'生成中' button near the input, animated "
    "dots, or rapidly changing text. Signals that it is done: a static "
    "reply with action buttons like 'Copy'/'复制', 'Regenerate'/'重新生成', "
    "thumbs-up/down, or the input field showing 'Send'/the normal "
    "placeholder again. "
    "Reply with ONE ```json``` fenced object: "
    '{"done": <true|false>, "text": "<the assistant\'s reply text verbatim, '
    'or null if you cannot read it>"} . '
    "Keep `text` short (<= 500 chars); summarize tail only if too long."
)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FENCE_ANY = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


# Common labels for in-app "stop generating" / "thinking" buttons. Used ONLY
# as a chrome-filter for the reply-text scrape (so e.g. "停止生成" doesn't
# leak into the extracted reply text). The done-detection signal is the
# text-hash diff in wait_for_reply Stage 2, not these markers.
_DEFAULT_STREAMING_MARKERS: tuple[str, ...] = (
    "停止生成", "停止回答", "停止", "生成中", "正在生成", "思考中",
    "Stop generating", "Stop", "Generating", "Thinking",
)


def _dump_window_xml_root(
    dump_timeout: float = 8, pull_timeout: float = 5,
) -> "ET.Element | None":
    """Run `uiautomator dump`, pull, parse. Returns root element or None on
    any failure (logged at info — dump can be flaky during animations).

    Timeouts are parameterized because the wait_for_reply precheck wants a
    tight budget (3s) — an 8s stall on every tick would burn the wall-clock
    budget when uiautomator is persistently unhealthy."""
    base = adb_base()
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
        local_xml = fh.name
    remote_xml = "/sdcard/appcards_window_dump.xml"
    try:
        dump = subprocess.run(
            base + ["shell", "uiautomator", "dump", remote_xml],
            capture_output=True, text=True, timeout=dump_timeout,
        )
        if dump.returncode != 0:
            return None
        pull = subprocess.run(
            base + ["pull", remote_xml, local_xml],
            capture_output=True, text=True, timeout=pull_timeout,
        )
        if pull.returncode != 0 or not os.path.getsize(local_xml):
            return None
        try:
            return ET.parse(local_xml).getroot()
        except ET.ParseError:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try:
            os.unlink(local_xml)
        except OSError:
            pass


def _dump_visible_text_hash(
    dump_timeout: float = 3,
    pull_timeout: float = 2,
) -> "str | None":
    """blake2b hash of all visible text + content-desc joined in document
    order. Used by the wait_for_reply Stage-2 precheck: if this tick's hash
    matches the previous tick's hash, no new text was rendered → the in-app
    agent is done streaming. If it differs, the reply is still growing → skip
    the VLM call. None on dump failure (caller falls through to VLM).

    This is strictly better than the old "look for 停止生成 marker" heuristic:
    app-agnostic (no per-app marker list to maintain), and it catches both
    apps without a stop button AND apps whose stop button stays around after
    generation completes."""
    root = _dump_window_xml_root(dump_timeout=dump_timeout, pull_timeout=pull_timeout)
    if root is None:
        return None
    parts: list[str] = []
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        d = (n.get("content-desc") or "").strip()
        if t:
            parts.append(t)
        if d and d != t:
            parts.append(d)
    joined = "␟".join(parts)
    import hashlib
    return hashlib.blake2b(joined.encode("utf-8", "replace"), digest_size=12).hexdigest()


# Chrome labels we never want to include in the extracted reply text.
# Combined with the streaming-marker list at runtime.
_REPLY_CHROME_LABELS: frozenset[str] = frozenset({
    "复制", "重新生成", "重试", "分享", "收藏", "点赞", "踩", "更多", "发送",
    "Copy", "Regenerate", "Retry", "Share", "Send", "More",
    "发消息", "发消息或按住说话", "请输入", "输入",
    "AI 内容仅供参考", "AI 生成内容可能存在错误",
})


def _extract_reply_text_from_dump(
    user_input_text: str | None,
    screen_h: int,
    extra_excludes: tuple[str, ...] = (),
) -> str | None:
    """Scrape the assistant's most recent reply text directly from the
    uiautomator XML, no VLM. Returns the joined text or None on dump failure
    / nothing plausibly-reply found.

    Heuristic (no per-app config needed for most chat UIs):
      1. Dump XML.
      2. Walk all visible text-bearing nodes in document order, recording
         (top-y, text). Strip status-bar (top 8%) and input-bar (bottom 18%)
         regions outright.
      3. If `user_input_text` was supplied and appears in any node, take the
         LAST such occurrence's y; keep only nodes whose top-y > that y.
         (Those are siblings rendered below the user's own bubble — i.e. the
         assistant's reply.)
      4. Filter out chrome labels (Copy / Regenerate / streaming markers /
         input placeholders).
      5. Join with newlines, return None if the result is empty/whitespace.
    """
    root = _dump_window_xml_root(dump_timeout=3, pull_timeout=2)
    if root is None:
        return None
    top_cutoff = int(screen_h * 0.08)
    bot_cutoff = int(screen_h * 0.82)
    # (top_y, text)
    nodes: list[tuple[int, str]] = []
    for n in root.iter("node"):
        t = (n.get("text") or "").strip()
        if not t:
            continue
        m = _BOUNDS_RE.match(n.get("bounds") or "")
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        if y2 <= y1 or x2 <= x1:
            continue
        # Drop status bar / input area / off-screen nodes.
        if y1 < top_cutoff or y1 > bot_cutoff:
            continue
        nodes.append((y1, t))
    if not nodes:
        return None
    # Find y of last occurrence of user's typed input (their own bubble).
    # We compare with substring containment to tolerate trailing spaces /
    # avatar timestamps appended by some apps.
    cut_y = -1
    if user_input_text:
        u = user_input_text.strip()
        if u:
            for y, t in nodes:
                if u in t or t in u:
                    cut_y = max(cut_y, y)
    # Filter: above user bubble OR known chrome OR streaming markers.
    excludes = set(_REPLY_CHROME_LABELS) | set(extra_excludes)
    excludes |= set(_DEFAULT_STREAMING_MARKERS)
    candidates: list[tuple[int, str]] = []
    for y, t in nodes:
        if cut_y >= 0 and y <= cut_y:
            continue
        if t in excludes:
            continue
        # Substring-match exclusion for noisy chrome variants ("AI 内容..." etc.)
        if any(x and x in t and len(x) >= 4 for x in excludes):
            continue
        candidates.append((y, t))
    if not candidates:
        return None
    # Drop short "quick-reply chip"-looking nodes IFF there's at least one
    # substantial node — otherwise a one-line reply would itself be dropped.
    # Threshold (25 chars) catches typical follow-up suggestion buttons
    # ("复旦大学有哪些王牌专业？") while preserving real reply prose.
    MIN_CHIP_LEN = 25
    has_substantial = any(len(t) >= MIN_CHIP_LEN for _, t in candidates)
    if has_substantial:
        candidates = [(y, t) for y, t in candidates if len(t) >= MIN_CHIP_LEN]
    joined = "\n".join(t for _, t in candidates).strip()
    return joined or None


def _hash_screenshot_region(image) -> str:
    """Perceptual-ish hash of the message area of a phone screenshot.
    Crops out the status bar (top ~8%) and the input/keyboard area (bottom
    ~18%) so a ticking clock or a blinking input caret doesn't constantly
    flip the hash. Downscales to 48×96 grayscale so a streaming cursor /
    small fading dots don't either, while a growing reply paragraph still
    changes enough pixels to register as different.

    This is the *fast* precheck signal in wait_for_reply: comparing this
    hash across ticks is essentially free, and lets us skip the expensive
    uiautomator dump (and the VLM call) while text is actively streaming."""
    w, h = image.size
    top = int(h * 0.08)
    bot = int(h * 0.82)
    crop = image.crop((0, top, w, bot))
    small = crop.convert("L").resize((48, 96))
    import hashlib
    return hashlib.blake2b(small.tobytes(), digest_size=12).hexdigest()


# --- System permission popup auto-dismiss ------------------------------------
#
# Per CLAUDE.md, every fresh capability that needs a runtime permission (camera,
# location, mic, contacts, ...) gets blocked by a system dialog like
# `要允许"千问"拍摄照片和录制视频吗？`. We auto-tap the most-permissive Allow
# button at the top of every `_materialize` so the planner doesn't have to know
# anything about it. Constraints:
#   * only fires when the FOREGROUND package is a known permission controller
#     — protects against an in-app "允许" label triggering a spurious tap.
#   * uses a cheap `dumpsys window` probe first (~200ms); only pays the full
#     uiautomator dump (~2.5s) when the probe says a permission UI is up.
#   * capped to MAX_DISMISSALS per task; a stuck dialog won't infinite-loop.
#   * env opt-out via APPCARDS_DISMISS_PERMISSIONS=0.
_PERMISSION_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.lbe.security.miui",        # MIUI / Xiaomi
    "com.miui.securitycenter",
    "com.huawei.systemmanager",     # Huawei / Honor
    "com.coloros.safecenter",       # OPPO
    "com.heytap.openid",            # OPPO/realme newer
    "com.vivo.permissionmanager",   # vivo
    "com.samsung.android.permissioncontroller",
)
# Preference order: most-permissive first so e.g. "始终允许" wins over "允许".
_ALLOW_LABELS: tuple[str, ...] = (
    "始终允许",
    "Always allow",
    "在使用应用时允许",
    "仅在使用该应用时允许",
    "使用该应用时允许",
    "While using the app",
    "Allow while using the app",
    "本次允许",
    "仅在本次使用允许",
    "Only this time",
    "允许",
    "Allow",
)
_FOCUS_PKG_RE = re.compile(r"\b([\w.]+)/[\w.$]+\b")


def _foreground_package() -> str | None:
    """Return the current foreground app's package id via `dumpsys window`.
    Cheap (~100-300ms). Returns None on any failure."""
    try:
        # Note: some Android builds (observed on this lab's pixel-class
        # device) emit nothing for `dumpsys window windows` — `dumpsys window`
        # without the subcommand returns the full state and is portable.
        r = subprocess.run(
            adb_base() + ["shell", "dumpsys", "window"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if "mCurrentFocus" not in line and "mFocusedApp" not in line:
                continue
            m = _FOCUS_PKG_RE.search(line)
            if m:
                return m.group(1)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _maybe_dismiss_permission_popup() -> str | None:
    """If a system permission/consent dialog is on top, tap the most-permissive
    Allow button. Returns the label tapped (for logging) or None when nothing
    was dismissed."""
    pkg = _foreground_package()
    if pkg is None or pkg not in _PERMISSION_PACKAGES:
        return None
    root = _dump_window_xml_root(dump_timeout=2, pull_timeout=1)
    if root is None:
        logger.info(
            f"permission popup probe: foreground={pkg!r} but uiautomator "
            "dump failed; cannot auto-dismiss"
        )
        return None
    # Walk allow labels in preference order; tap the first match. Restrict to
    # nodes belonging to a permission package (the dump can include overlays
    # from other system surfaces).
    for label in _ALLOW_LABELS:
        for n in root.iter("node"):
            if (n.get("package") or "") not in _PERMISSION_PACKAGES:
                continue
            t = (n.get("text") or "").strip()
            d = (n.get("content-desc") or "").strip()
            if t != label and d != label:
                continue
            if (n.get("clickable") or "").lower() != "true":
                continue
            m = _BOUNDS_RE.match(n.get("bounds") or "")
            if not m:
                continue
            x1, y1, x2, y2 = (int(v) for v in m.groups())
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            try:
                subprocess.run(
                    adb_base() + ["shell", "input", "tap", str(cx), str(cy)],
                    capture_output=True, text=True, timeout=3,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning(f"permission popup tap failed: {e}")
                return None
            logger.info(
                f"dismissed system permission popup: tapped {label!r} at "
                f"({cx},{cy}) on {pkg}"
            )
            return label
    logger.warning(
        f"permission popup probe: foreground={pkg!r} but no known Allow "
        f"button found in dump (tried {len(_ALLOW_LABELS)} labels)"
    )
    return None


# Strip whitespace + common punctuation noise so two VLM extractions of the
# same paragraph compare equal even when one renders "2022年, 董..." and the
# other "2022年，董...", or with/without inline numbering / bullet glyphs.
_DEDUP_STRIP_RE = re.compile(r"[\s.,;:!?，。、；：！？\-—–·•*•]+")


def _normalize_for_dedup(s: str) -> str:
    """Lowercase + drop whitespace and minor punctuation. Used only for
    chunk-equality checks; the original chunk text is preserved for output."""
    return _DEDUP_STRIP_RE.sub("", s).lower()


def _stitch_chunks(chunks: list[str]) -> str:
    """Merge VLM-extracted chunks from sliding screenshot windows into one
    coherent reply. Two passes:

      1. Drop any chunk whose normalized form is a substring of another
         (sub-window dupes — same content captured at a slightly different
         scroll position).
      2. Walk surviving chunks in capture order (top → bottom) and append
         their lines, skipping any line whose normalized form was already
         emitted. This handles both the heavy-overlap case (most lines
         dedupe → output ≈ longest chunk) and the disjoint-content case
         (chunks cover different parts of a long reply → output is the
         union, in reading order).

    Char-level suffix/prefix stitching is intentionally avoided: VLM
    paraphrase drift defeats it. Line-level dedup is robust because the
    VLM tends to reproduce whole lines verbatim per frame even when the
    surrounding wrap changes.

    Chunks are assumed to be in reading order (top → bottom)."""
    chunks = [c for c in chunks if c and c.strip()]
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]

    # (1) Drop substring duplicates (normalized).
    norms = [_normalize_for_dedup(c) for c in chunks]
    keep_idx: list[int] = []
    for i, ni in enumerate(norms):
        if not ni:
            continue
        if any(i != j and norms[j] and ni in norms[j] for j in range(len(norms))):
            continue  # ni is a substring of some other chunk
        keep_idx.append(i)
    chunks = [chunks[i] for i in keep_idx]
    if len(chunks) <= 1:
        return chunks[0] if chunks else ""

    # (2) Line-level ordered-dedup merge. For each chunk in capture order,
    # append its lines unless we've already emitted that line (normalized).
    # Blank lines pass through unconditionally so paragraph breaks survive,
    # but consecutive blanks are collapsed.
    out_lines: list[str] = []
    seen: set[str] = set()
    new_line_counts: list[int] = []
    for c in chunks:
        added = 0
        for line in c.splitlines():
            if not line.strip():
                if out_lines and out_lines[-1].strip():
                    out_lines.append("")
                continue
            key = _normalize_for_dedup(line)
            if not key or key in seen:
                continue
            seen.add(key)
            out_lines.append(line)
            added += 1
        new_line_counts.append(added)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    merged = "\n".join(out_lines)
    logger.info(
        f"_stitch_chunks: merged {len(chunks)} chunks by line-dedup "
        f"(new lines per chunk: {new_line_counts}) -> {len(merged)} chars"
    )
    return merged


def _ground_text_via_uiautomator(
    target: str, screen_w: int, screen_h: int
) -> tuple[int, int] | None:
    """Dump the current UI via `uiautomator dump` and find a node whose
    text / content-desc / resource-id matches `target`. Returns the center of
    the matching node's bounds in screen pixels, or None on miss.

    Match policy (tightest first):
      1. exact text or content-desc match
      2. substring match (text contains target, or vice versa)
      3. resource-id endswith target

    All matches are restricted to clickable / focusable / visible nodes when
    possible — falls back to any node if no clickable match exists.
    """
    base = adb_base()

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
        local_xml = fh.name
    remote_xml = "/sdcard/appcards_window_dump.xml"
    try:
        dump = subprocess.run(
            base + ["shell", "uiautomator", "dump", remote_xml],
            capture_output=True, text=True, timeout=8,
        )
        if dump.returncode != 0:
            logger.warning(f"uiautomator dump failed: {dump.stderr.strip()}")
            return None
        pull = subprocess.run(
            base + ["pull", remote_xml, local_xml],
            capture_output=True, text=True, timeout=5,
        )
        if pull.returncode != 0 or not os.path.getsize(local_xml):
            logger.warning(f"adb pull failed: {pull.stderr.strip()}")
            return None
        try:
            root = ET.parse(local_xml).getroot()
        except ET.ParseError as e:
            logger.warning(f"window dump XML parse error: {e}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"uiautomator path unavailable: {e}")
        return None
    finally:
        try:
            os.unlink(local_xml)
        except OSError:
            pass

    nodes = list(root.iter("node"))

    def _bounds_center(node) -> tuple[int, int] | None:
        m = _BOUNDS_RE.match(node.get("bounds") or "")
        if not m:
            return None
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        # Filter zero-area or off-screen rectangles.
        if x2 <= x1 or y2 <= y1:
            return None
        if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
            return None
        return cx, cy

    def _candidates(predicate) -> list[tuple[int, tuple[int, int]]]:
        out: list[tuple[int, tuple[int, int]]] = []
        for n in nodes:
            if not predicate(n):
                continue
            c = _bounds_center(n)
            if c is None:
                continue
            # Prefer clickable / focusable nodes — higher score = better.
            score = 0
            if (n.get("clickable") or "").lower() == "true":
                score += 4
            if (n.get("focusable") or "").lower() == "true":
                score += 2
            if (n.get("enabled") or "").lower() == "true":
                score += 1
            out.append((score, c))
        out.sort(reverse=True)
        return out

    def _attr(n, k):
        return n.get(k) or ""

    # Tier 1: exact text or content-desc
    hits = _candidates(
        lambda n: _attr(n, "text") == target or _attr(n, "content-desc") == target
    )
    # Tier 2: substring either way
    if not hits:
        hits = _candidates(
            lambda n: (target in _attr(n, "text") and _attr(n, "text"))
            or (target in _attr(n, "content-desc") and _attr(n, "content-desc"))
            or (_attr(n, "text") and _attr(n, "text") in target and len(_attr(n, "text")) > 1)
        )
    # Tier 3: resource-id endswith
    if not hits:
        hits = _candidates(
            lambda n: _attr(n, "resource-id").split("/")[-1] == target
        )
    if not hits:
        logger.info(
            f"uiautomator dump ok ({len(nodes)} nodes) but no match for "
            f"{target!r}"
        )
        return None
    logger.info(
        f"uiautomator hit for {target!r}: bounds-center={hits[0][1]} "
        f"(score={hits[0][0]}, {len(hits)} candidates)"
    )
    return hits[0][1]


def _extract_xy(raw: str) -> tuple[int | None, int | None]:
    """Tolerant extractor for VLM grounding outputs.

    Handles, in order of preference:
      - {"x": <int>, "y": <int>}                                (spec)
      - {"point": [x, y]} / {"bbox": [x1,y1,x2,y2]}             (some VLMs)
      - [{"x": [x, y]}, ...]  (Qwen-VL: 'x' field holds [x, y]) (Qwen-VL)
      - [[x, y]] / [x, y]                                       (raw point)
    Falls back to a regex over the first two integers if all else fails.
    """
    import ast

    # 1. Try the spec-shaped fenced object first.
    m = _JSON_FENCE.search(raw)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "x" in d and "y" in d and not isinstance(d["x"], list):
                return d["x"], d["y"]
        except json.JSONDecodeError:
            pass

    # 2. Otherwise grab whatever is inside any fenced block, or the raw text.
    m2 = _FENCE_ANY.search(raw)
    payload = (m2.group(1) if m2 else raw).strip()

    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            data = loader(payload)
            break
        except (json.JSONDecodeError, ValueError, SyntaxError):
            continue

    def _unwrap(d):
        if isinstance(d, dict):
            # {"x": int, "y": int}
            if isinstance(d.get("x"), (int, float)) and isinstance(d.get("y"), (int, float)):
                return int(d["x"]), int(d["y"])
            # Qwen-VL: {"x": [x, y]}
            if isinstance(d.get("x"), (list, tuple)) and len(d["x"]) >= 2:
                return int(d["x"][0]), int(d["x"][1])
            # {"point": [x, y]} / {"coordinate": [x, y]}
            for k in ("point", "coordinate", "coordinates", "position", "center"):
                v = d.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    return int(v[0]), int(v[1])
            # {"bbox": [x1, y1, x2, y2]} → center
            for k in ("bbox", "bbox_2d", "box"):
                v = d.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 4:
                    return int((v[0] + v[2]) / 2), int((v[1] + v[3]) / 2)
            # {"x": null, "y": null} → not found
            if "x" in d and "y" in d and d["x"] is None:
                return None, None
        return None

    if isinstance(data, list) and data:
        head = data[0]
        if isinstance(head, (int, float)) and len(data) >= 2:
            return int(data[0]), int(data[1])
        if isinstance(head, (list, tuple)) and len(head) >= 2:
            return int(head[0]), int(head[1])
        if isinstance(head, dict):
            r = _unwrap(head)
            if r is not None:
                return r
    if isinstance(data, dict):
        r = _unwrap(data)
        if r is not None:
            return r

    # 3. Last-ditch: pull the first two integers out of the text.
    nums = re.findall(r"-?\d+", payload)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None, None


def _sanitize_messages_for_log(messages: list[dict]) -> list[dict]:
    """Strip giant base64 image_url payloads so traj.json stays readable."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append({"type": "raw", "value": repr(part)[:200]})
                    continue
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if isinstance(url, str) and url.startswith("data:"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"<base64 image, {len(url)} chars>"
                        }})
                    else:
                        parts.append(part)
                else:
                    parts.append(part)
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out


def _sanitize_kwargs_for_log(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items()
            if k in ("temperature", "max_tokens", "max_completion_tokens", "stream")}


def _llm_purpose_from_messages(messages: list[dict]) -> str:
    """Best-effort label for a call site (capability-router / grounding /
    reply-watch / other), inferred from the system prompt."""
    if not messages:
        return "unknown"
    sys_msg = next((m for m in messages if m.get("role") == "system"), None)
    sys = (sys_msg or {}).get("content")
    if not isinstance(sys, str):
        return "unknown"
    if sys.startswith(_GROUNDING_SYSTEM[:40]):
        return "grounding"
    if sys.startswith(_REPLY_WATCH_SYSTEM[:40]):
        return "reply_watch"
    if "capability id" in sys:
        return "capability_router"
    return "other"


class AppCardsAgent(MCPAgent):
    """Card-driven agent. The model only picks capabilities and grounds text
    selectors; tap coordinates come from the card's `x_bounds`."""

    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(tools=tools or [], **kwargs)
        self.model_name = model_name
        self.llm_base_url = llm_base_url
        self.api_key = api_key
        self.build_openai_client(self.llm_base_url, self.api_key)

        self.target_app: str | None = os.getenv(_TARGET_APP_ENV)
        self.manifests_dir = (
            Path(os.environ[_MANIFESTS_ENV])
            if os.getenv(_MANIFESTS_ENV)
            else None
        )
        self.target_density: int | None = (
            int(os.environ[_DENSITY_ENV]) if os.getenv(_DENSITY_ENV) else None
        )

        self.card: dict | None = None
        self.plan: list[Step] = []
        self.cursor: int = 0
        self._planned: bool = False
        self._reply_polls: int = 0
        self._reply_precheck_skips: int = 0
        self._reply_precheck_skips_since_vlm: int = 0
        self._reply_dump_fail_streak: int = 0
        self._reply_precheck_disabled: bool = False
        self._reply_last_shot_hash: str | None = None
        self._reply_last_dump_text_hash: str | None = None
        self._reply_stable_streak: int = 0
        self._reply_start_ts: float | None = None
        self._wait_text_start_ts: float | None = None
        self._last_agent_reply: str | None = None
        self._last_input_text: str | None = None
        # Multi-screen capture state for replies that exceed one viewport
        # (e.g. 小红书 点点 returns long answers with stacked POI cards). See
        # the `capture_full` branch in wait_for_reply.
        self._capture_phase: str | None = None  # None | "scrolling"
        self._captured_chunks: list[str] = []
        self._capture_scrolls: int = 0
        self._capture_idle: int = 0
        self.fresh_conversation: bool = os.getenv(_FRESH_CONV_ENV, "1") != "0"
        self.skip_open_app: bool = os.getenv(_SKIP_OPEN_APP_ENV, "0") == "1"
        self.dismiss_permissions: bool = os.getenv(_DISMISS_PERMS_ENV, "1") != "0"
        self._permission_dismissed_count: int = 0

    def openai_chat_completions_create(  # type: ignore[override]
        self,
        model: str,
        messages: list[dict],
        **kwargs: Any,
    ) -> str | None:
        """Wrap MCPAgent's LLM call so every invocation is appended to
        traj.json. Image payloads are replaced with a short placeholder so the
        log stays human-readable; token deltas are computed from MCPAgent's
        running totals so we record per-call usage."""
        started = time.monotonic()
        pre_completion = self._total_completion_tokens
        pre_prompt = self._total_prompt_tokens
        pre_cached = self._total_cached_tokens
        purpose = _llm_purpose_from_messages(messages)
        try:
            raw = super().openai_chat_completions_create(
                model=model, messages=messages, **kwargs
            )
        except Exception as e:  # pragma: no cover — best-effort logging
            self._append_llm_call({
                "ts": time.time(),
                "elapsed_s": round(time.monotonic() - started, 3),
                "purpose": purpose,
                "model": model,
                "messages": _sanitize_messages_for_log(messages),
                "response": None,
                "error": repr(e),
                "plan_step": self.cursor if self._planned else None,
                "kwargs": _sanitize_kwargs_for_log(kwargs),
            })
            raise
        self._append_llm_call({
            "ts": time.time(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "purpose": purpose,
            "model": model,
            "messages": _sanitize_messages_for_log(messages),
            "response": raw,
            "usage_delta": {
                "completion_tokens": self._total_completion_tokens - pre_completion,
                "prompt_tokens": self._total_prompt_tokens - pre_prompt,
                "cached_tokens": self._total_cached_tokens - pre_cached,
            },
            "plan_step": self.cursor if self._planned else None,
            "kwargs": _sanitize_kwargs_for_log(kwargs),
        })
        return raw

    def _append_llm_call(self, record: dict) -> None:
        """Append one LLM-call record to traj_logs/user_task/traj.json under
        log_data["0"]["llm_calls"]. Defensive: creates the bucket and stub
        traj/tools fields so MW's first log_traj does not KeyError on them."""
        traj_path = _TRAJ_DIR / "traj.json"
        try:
            if not traj_path.exists():
                return
            try:
                with open(traj_path, encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                # Either a fresh `{}` mid-write or a corrupted file. Skip this
                # record rather than clobber MW's writer.
                return
            if not isinstance(data, dict):
                return
            bucket = data.setdefault("0", {})
            bucket.setdefault("tools", None)
            bucket.setdefault("traj", [])
            bucket.setdefault("llm_calls", []).append(record)
            with open(traj_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except OSError as e:
            logger.warning(f"Failed to append LLM call to traj.json: {e}")

    def initialize_hook(self, instruction: str) -> None:
        logger.info(f"AppCardsAgent init: instruction={instruction!r}")
        if not self.target_app:
            raise RuntimeError(
                f"{_TARGET_APP_ENV} must be set to the target app's package id "
                "(e.g. com.aliyun.tongyi)."
            )
        self.card = load_card_by_app_id(self.target_app, self.manifests_dir)
        logger.info(
            f"Loaded card {self.card['app_id']} v{self.card.get('card_version')} "
            f"({self.card['app_name']})"
        )
        self.plan = []
        self.cursor = 0
        self._planned = False

    def reset(self) -> None:
        self.card = None
        self.plan = []
        self.cursor = 0
        self._planned = False
        self._reply_polls = 0
        self._reply_precheck_skips = 0
        self._reply_precheck_skips_since_vlm = 0
        self._reply_dump_fail_streak = 0
        self._reply_precheck_disabled = False
        self._reply_last_shot_hash = None
        self._reply_last_dump_text_hash = None
        self._reply_stable_streak = 0
        self._reply_start_ts = None
        self._wait_text_start_ts = None
        self._last_agent_reply = None
        self._last_input_text = None
        self._capture_phase = None
        self._captured_chunks = []
        self._capture_scrolls = 0
        self._capture_idle = 0
        self._permission_dismissed_count = 0

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        screenshot = observation["screenshot"]
        screen_w, screen_h = screenshot.size

        if not self._planned:
            cap_id, invocation = route_capability(self, self.instruction, self.card)
            self.plan = build_plan(
                self.card,
                cap_id,
                invocation,
                fresh_conversation=self.fresh_conversation,
                skip_open_app=self.skip_open_app,
            )
            self._planned = True
            logger.info(
                f"Plan ({len(self.plan)} steps) for capability={cap_id!r}: "
                + " → ".join(f"{s.kind}" for s in self.plan)
            )

        # System permission popup hook — runs BEFORE the planned step. If a
        # known permission controller is foreground we tap the most-permissive
        # Allow, return a no-op wait that does NOT advance the cursor, and let
        # MW capture a fresh screenshot. Next predict re-enters cleanly with
        # the popup gone. Bounded so a stuck dialog can't infinite-loop.
        MAX_DISMISSALS = 8
        if (
            self.dismiss_permissions
            and self._permission_dismissed_count < MAX_DISMISSALS
        ):
            label = _maybe_dismiss_permission_popup()
            if label is not None:
                self._permission_dismissed_count += 1
                thought = (
                    f"system permission popup: tapped {label!r} "
                    f"(#{self._permission_dismissed_count}/{MAX_DISMISSALS})"
                )
                logger.info(thought)
                return thought, JSONAction(action_type="wait")

        if self.cursor >= len(self.plan):
            return ("plan exhausted", JSONAction(action_type="finished", goal_status="complete"))

        step_idx = self.cursor  # 0-based index of the step we're about to run
        step = self.plan[step_idx]
        action, advance, extra_note = self._materialize(step, screenshot, screen_w, screen_h)
        if advance:
            self.cursor += 1
        note = step.note + (f"; {extra_note}" if extra_note else "")
        # Display 1-based index of the CURRENT step (the one we just emitted
        # an action for), not the next one. wait_for_reply re-enters the same
        # index until it advances, which is fine and visible in the suffix.
        suffix = "" if advance else " [hold]"
        thought = (
            f"step {step_idx + 1}/{len(self.plan)}: {step.kind} ({note}){suffix}"
        )
        logger.info(f"{thought} → {action.model_dump(exclude_none=True)}")
        return thought, action

    def _materialize(
        self,
        step: Step,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[JSONAction, bool, str]:
        """Return (action, advance_cursor, extra_note)."""
        kind = step.kind
        p = step.payload

        if kind == "open_app":
            # Cold-launch policy: always force-stop before launching so the
            # in-app agent observes a clean home surface. MobileWorld's
            # `open_app` is launcher-tap-based and does NOT force-stop, so we
            # do it ourselves. The run_test.py / flow_runner wrappers do the
            # FULL cold-launch (force-stop + monkey LAUNCHER) before reaching
            # this code path; duplicating force-stop here covers direct
            # `mw test` invocations that bypass those wrappers — MobileWorld
            # will perform the launcher tap itself via the returned action.
            pkg = p["package"]
            try:
                force_stop(pkg)
            except Exception as e:  # pragma: no cover — best-effort
                logger.warning(f"force-stop {pkg} failed (continuing): {e}")
            # MobileWorld's open_app expects the launcher label (e.g. "千问"),
            # not the package id. Prefer the card's embedded_agent.name as the
            # launcher label; fall back to app_name, then the package id.
            launcher_label = (
                (self.card or {}).get("embedded_agent", {}).get("name")
                or (self.card or {}).get("app_name")
                or pkg
            )
            return JSONAction(action_type="open_app", app_name=launcher_label), True, ""

        if kind == "tap_bounds":
            x, y = bounds_center(
                p["bounds"], self.card, (screen_w, screen_h), self.target_density
            )
            return JSONAction(action_type="click", x=x, y=y), True, ""

        if kind == "tap_fraction":
            return JSONAction(
                action_type="click",
                x=int(p["x_ratio"] * screen_w),
                y=int(p["y_ratio"] * screen_h),
            ), True, ""

        if kind == "tap_text":
            # 1. Try uiautomator XML first (precise, free, robust to UI redraws).
            #    Retry briefly to absorb animation latency (drawer open, etc.).
            # 2. Fall back to the VLM only if the text was not in the a11y tree.
            xy = None
            for attempt in range(3):
                xy = _ground_text_via_uiautomator(p["text"], screen_w, screen_h)
                if xy is not None:
                    break
                if attempt < 2:
                    time.sleep(0.8)
            if xy is not None:
                x, y = xy
                note = "uiautomator"
            else:
                x, y = self._ground_text(p["text"], screenshot, screen_w, screen_h)
                note = "VLM"
            return JSONAction(action_type="click", x=x, y=y), True, note

        if kind == "input_text":
            # Save the typed text so the reply-extraction heuristic in
            # wait_for_reply can use it to locate the user's own bubble in
            # the message list (everything visually BELOW is the reply).
            self._last_input_text = p["text"]
            return JSONAction(action_type="input_text", text=p["text"], clear_text=True), True, ""

        if kind == "wait_ms":
            return JSONAction(action_type="wait"), True, ""

        if kind == "wait_text":
            # Poll uiautomator until `text` shows up or timeout elapses. Each
            # call to _materialize is one tick of MobileWorld's step loop;
            # we hold the cursor (advance=False) while waiting so subsequent
            # ticks re-enter this branch.
            target = p.get("text") or ""
            timeout_ms = int(p.get("timeout_ms", 5000))
            if not target:
                return JSONAction(action_type="wait"), True, "no text; bare wait"
            if self._wait_text_start_ts is None:
                self._wait_text_start_ts = time.monotonic()
            hit = _ground_text_via_uiautomator(target, screen_w, screen_h)
            elapsed_ms = int((time.monotonic() - self._wait_text_start_ts) * 1000)
            if hit is not None:
                logger.info(
                    f"wait_text: {target!r} appeared after {elapsed_ms}ms"
                )
                self._wait_text_start_ts = None
                return JSONAction(action_type="wait"), True, (
                    f"text {target!r} present ({elapsed_ms}ms)"
                )
            if elapsed_ms >= timeout_ms:
                logger.warning(
                    f"wait_text: {target!r} did not appear within "
                    f"{timeout_ms}ms; advancing anyway"
                )
                self._wait_text_start_ts = None
                return JSONAction(action_type="wait"), True, (
                    f"timeout after {elapsed_ms}ms"
                )
            return JSONAction(action_type="wait"), False, (
                f"waiting for {target!r} ({elapsed_ms}ms/{timeout_ms}ms)"
            )

        if kind == "wait_for_reply":
            capture_full = bool(p.get("capture_full"))
            max_capture_scrolls = int(p.get("max_capture_scrolls", 6))

            # Phase 2: after done, walk through the rest of the reply by
            # swiping the visible portion off so the next chunk slides into
            # view; capture each frame's reply text. Stops on max scrolls or
            # when two consecutive frames produce no new text.
            #
            # We prefer the uiautomator scrape over a VLM call per frame —
            # the scrape is free (no tokens) and returns the full visible
            # text verbatim. VLM is only used as a fallback when the scrape
            # finds nothing (e.g. WebView-rendered replies whose text isn't
            # in the a11y tree).
            if self._capture_phase == "scrolling":
                text = _extract_reply_text_from_dump(
                    self._last_input_text, screen_h
                )
                source = "scrape"
                if not text:
                    _, text = self._poll_agent_reply(screenshot)
                    source = "vlm_fallback"
                # Substring dedup with normalization: a new VLM-extracted
                # frame often repeats text from a previous frame but with
                # tiny formatting drift (whitespace, punctuation, markdown
                # numbering style). Comparing on a normalized form (no
                # whitespace, no punctuation noise) catches those duplicates
                # while we keep the richer original text for storage. If the
                # new chunk strictly EXTENDS an existing one, replace in
                # place so we end up with the longest variant.
                novel = False
                if text:
                    n_text = _normalize_for_dedup(text)
                    norms = [_normalize_for_dedup(c) for c in self._captured_chunks]
                    contained = any(n_text and n_text in nc for nc in norms)
                    if not contained:
                        replaced = False
                        for i, nc in enumerate(norms):
                            if nc and nc in n_text:
                                # New chunk is a superset — keep the longer one.
                                self._captured_chunks[i] = text
                                replaced = True
                                break
                        if not replaced:
                            self._captured_chunks.append(text)
                        novel = True
                if novel:
                    self._capture_idle = 0
                    logger.info(
                        f"Capture scroll {self._capture_scrolls}: +chunk "
                        f"({len(text)} chars, via {source})"
                    )
                else:
                    self._capture_idle += 1
                stop = (
                    self._capture_scrolls >= max_capture_scrolls
                    or self._capture_idle >= 2
                )
                if stop:
                    # Chunks were captured top→bottom in reading order;
                    # stitch adjacent chunks at their suffix/prefix overlap
                    # so duplicated seam content collapses.
                    full = _stitch_chunks(list(self._captured_chunks))
                    self._last_agent_reply = full
                    logger.info(
                        f"Reply capture complete: {len(self._captured_chunks)} "
                        f"chunks, {len(full)} chars total"
                    )
                    self._capture_phase = None
                    self._captured_chunks = []
                    self._capture_scrolls = 0
                    self._capture_idle = 0
                    return JSONAction(action_type="wait"), True, "capture done"
                self._capture_scrolls += 1
                # Issue our own larger-than-default swipe (MW's built-in
                # scroll is fixed at ~0.4*width vertical, which means many
                # frames + many VLM calls). Then return a no-op so MW just
                # captures the next screenshot.
                swipe_down()
                return (
                    JSONAction(action_type="wait"),
                    False,
                    f"capture scroll {self._capture_scrolls}/{max_capture_scrolls}",
                )

            # Phase 1: poll for done. Budget is WALL-CLOCK seconds, not poll
            # count — each poll is a real VLM call (multiple seconds), so a
            # poll-count budget under-reports actual latency wildly.
            if self._reply_start_ts is None:
                self._reply_start_ts = time.monotonic()
            max_seconds = max(1, int(p.get("max_seconds", 30)))
            elapsed = time.monotonic() - self._reply_start_ts

            # Two-stage pre-check before paying for a VLM call. Stage 1 is
            # essentially free; stage 2 only fires when stage 1 says the
            # screen is stable.
            #
            #  Stage 1 — screenshot hash diff over the message-area crop
            #    (see _hash_screenshot_region). Streaming text → pixels change
            #    → hash flips → SKIP both the dump and the VLM. This is the
            #    common case during the first few seconds of a reply and
            #    cuts the bulk of dump cost.
            #
            #  Stage 2 — once the screen has been stable for one tick,
            #    uiautomator dump and HASH the visible text. Compare to the
            #    previous dump's text hash:
            #      - first dump (no baseline): fall through to VLM
            #      - hash CHANGED: text is still growing → skip VLM
            #      - hash STABLE: two consecutive dumps with identical text →
            #        call VLM to confirm done + extract text
            #    This replaces the old "look for 停止生成 marker" heuristic,
            #    which was brittle (not every app has a stop button; some
            #    apps' stop buttons stay around after generation completes).
            #    Text-diff is app-agnostic and directly measures the signal
            #    we actually care about.
            #
            # Hardening so a broken uiautomator can't strand us:
            #  * CIRCUIT BREAKER — 2 consecutive dump failures disable the
            #    stage-2 dump for the rest of this wait_for_reply; once the
            #    screen is stable we go straight to VLM.
            #  * WATCHDOG — force a VLM poll after MAX_SKIPS_BEFORE_FORCE
            #    consecutive precheck skips, so an animated UI element that
            #    keeps flipping the hash cannot block detection of done.
            MAX_DUMP_FAILS = 2
            MAX_SKIPS_BEFORE_FORCE = 5
            force_vlm = (
                self._reply_precheck_skips_since_vlm >= MAX_SKIPS_BEFORE_FORCE
            )
            if not force_vlm and elapsed < max_seconds:
                # Stage 1: free screenshot hash.
                shot_hash = _hash_screenshot_region(screenshot)
                shot_changed = shot_hash != self._reply_last_shot_hash
                self._reply_last_shot_hash = shot_hash
                if shot_changed:
                    # Don't even dump — pixels are mutating.
                    self._reply_stable_streak = 0
                    self._reply_precheck_skips += 1
                    self._reply_precheck_skips_since_vlm += 1
                    time.sleep(0.8)
                    return (
                        JSONAction(action_type="wait"),
                        False,
                        (
                            f"precheck skip #{self._reply_precheck_skips} "
                            f"(screen changed) @ {elapsed:.1f}s/{max_seconds}s"
                        ),
                    )
                # Screen pixels are stable — but pixel-stability at 48×96
                # downscale can miss small text growth. Stage 2 is the
                # semantic check.
                self._reply_stable_streak += 1

                # Stage 2: dump and hash visible text. Skipped if breaker
                # tripped — stable screens then go straight to VLM.
                if not self._reply_precheck_disabled:
                    text_hash = _dump_visible_text_hash()
                    if text_hash is None:
                        self._reply_dump_fail_streak += 1
                        if self._reply_dump_fail_streak >= MAX_DUMP_FAILS:
                            self._reply_precheck_disabled = True
                            logger.warning(
                                "wait_for_reply stage-2 dump disabled for "
                                f"this wait — {self._reply_dump_fail_streak} "
                                "consecutive dump failures; stable screens "
                                "will go straight to VLM"
                            )
                    else:
                        self._reply_dump_fail_streak = 0
                        prev = self._reply_last_dump_text_hash
                        self._reply_last_dump_text_hash = text_hash
                        # Skip VLM only when we have a baseline AND the text
                        # changed since last tick — that's the "still growing"
                        # signal. First dump (no baseline) or unchanged text
                        # → fall through to VLM (it's the authoritative done
                        # judge; pixel-stable + text-stable is when it lands).
                        if prev is not None and text_hash != prev:
                            self._reply_precheck_skips += 1
                            self._reply_precheck_skips_since_vlm += 1
                            time.sleep(0.8)
                            return (
                                JSONAction(action_type="wait"),
                                False,
                                (
                                    f"precheck skip #{self._reply_precheck_skips} "
                                    f"(text still growing) "
                                    f"@ {elapsed:.1f}s/{max_seconds}s"
                                ),
                            )

            if force_vlm:
                logger.info(
                    f"wait_for_reply watchdog: forcing VLM poll after "
                    f"{self._reply_precheck_skips_since_vlm} consecutive "
                    "precheck skips"
                )
            done, text = self._poll_agent_reply(screenshot)
            self._reply_polls += 1
            self._reply_precheck_skips_since_vlm = 0
            elapsed = time.monotonic() - self._reply_start_ts
            # Trust `done` only if the VLM also produced text. If text is None,
            # the VLM is telling us it cannot read any reply on screen — which
            # almost always means generation has not actually finished. Keep
            # polling until we either get text or hit the timeout.
            if done and text:
                # VLM said done and gave us a text snippet. Try to UPGRADE that
                # text via direct uiautomator scrape — the VLM is asked to cap
                # at 500 chars, and on long replies it summarizes the tail. The
                # scrape returns the full visible text verbatim, no token cost.
                scraped = _extract_reply_text_from_dump(
                    self._last_input_text, screen_h
                )
                if scraped and len(scraped) > len(text):
                    logger.info(
                        f"reply text upgrade: VLM={len(text)} chars → "
                        f"uiautomator scrape={len(scraped)} chars"
                    )
                    text = scraped
                self._last_agent_reply = text
                logger.info(
                    f"In-app agent reply DONE after {self._reply_polls} poll(s) "
                    f"({self._reply_precheck_skips} precheck skip(s) saved) "
                    f"/ {elapsed:.1f}s; text={text!r}"
                )
                self._reply_polls = 0
                self._reply_precheck_skips = 0
                self._reply_precheck_skips_since_vlm = 0
                self._reply_dump_fail_streak = 0
                self._reply_precheck_disabled = False
                self._reply_last_shot_hash = None
                self._reply_last_dump_text_hash = None
                self._reply_stable_streak = 0
                self._reply_start_ts = None
                if capture_full:
                    self._capture_phase = "scrolling"
                    self._captured_chunks = [text]
                    self._capture_scrolls = 0
                    self._capture_idle = 0
                    swipe_down()
                    return (
                        JSONAction(action_type="wait"),
                        False,
                        "done; entering full-reply capture",
                    )
                return JSONAction(action_type="wait"), True, f"done; text={text!r}"
            if done and not text:
                logger.warning(
                    f"VLM reported done but returned no text on poll "
                    f"{self._reply_polls} ({elapsed:.1f}s/{max_seconds}s) — "
                    "distrusting, continuing"
                )
            if elapsed >= max_seconds:
                # Same upgrade as the happy path — VLM truncates at ~500 chars
                # and on long-running replies that hit the timeout the scrape
                # almost always has more (and verbatim) content.
                scraped = _extract_reply_text_from_dump(
                    self._last_input_text, screen_h
                )
                if scraped and (not text or len(scraped) > len(text)):
                    logger.info(
                        f"reply text upgrade on timeout: "
                        f"VLM={len(text) if text else 0} chars → "
                        f"uiautomator scrape={len(scraped)} chars"
                    )
                    text = scraped
                logger.warning(
                    f"In-app agent reply did not finish within {max_seconds}s "
                    f"({self._reply_polls} poll(s)); advancing anyway "
                    f"(last text={text!r})"
                )
                self._last_agent_reply = text
                self._reply_polls = 0
                self._reply_precheck_skips = 0
                self._reply_precheck_skips_since_vlm = 0
                self._reply_dump_fail_streak = 0
                self._reply_precheck_disabled = False
                self._reply_last_shot_hash = None
                self._reply_last_dump_text_hash = None
                self._reply_stable_streak = 0
                self._reply_start_ts = None
                return JSONAction(action_type="wait"), True, "timeout"
            return (
                JSONAction(action_type="wait"),
                False,
                (
                    f"poll {self._reply_polls} @ {elapsed:.1f}s/{max_seconds}s "
                    f"(+{self._reply_precheck_skips} precheck skips)"
                ),
            )

        if kind == "tap_unless_present":
            # Probe via uiautomator only (cheap + precise); fall through to
            # tap target if probe is missing. We deliberately do NOT fall
            # back to VLM for the probe — a VLM hallucination here would
            # cause a destructive tap on a non-idempotent UI toggle.
            probe = p["probe"]
            target = p["target"]
            probe_text = probe.get("text") or probe.get("text_contains")
            if probe_text and _ground_text_via_uiautomator(
                probe_text, screen_w, screen_h
            ) is not None:
                return JSONAction(action_type="wait"), True, (
                    f"probe {probe_text!r} present; skipping conditional tap"
                )
            # Probe missing → tap target. Only x_bounds supported here to
            # keep the conditional-tap semantics deterministic.
            if "x_bounds" not in target:
                logger.warning(
                    f"tap_unless_present: unsupported target {target!r}; "
                    "only x_bounds is implemented. Skipping."
                )
                return JSONAction(action_type="wait"), True, "unsupported target"
            x, y = bounds_center(
                target["x_bounds"], self.card, (screen_w, screen_h), self.target_density
            )
            return JSONAction(action_type="click", x=x, y=y), True, (
                f"probe {probe_text!r} absent; tapping target bounds"
            )

        if kind == "swipe":
            return JSONAction(action_type="scroll", direction=p.get("direction", "down")), True, ""

        if kind == "copy_reply":
            # Single-shot: tap the in-app 复制 button. We don't read the
            # clipboard back — Android's Binder 1MB cap rejects WeChat AI 搜索
            # copies (they include cited cards + HTML). The answer is left on
            # the device clipboard for the user / a downstream IME helper.
            #
            # Locator priority:
            #   1. VLM grounding via `text`
            #   2. Sanity-check the (x,y) against `valid_x` / `valid_y`. The
            #      copy icon is in a fixed COLUMN on this device — only the
            #      toolbar's y drifts with reply length — so a wildly off x
            #      is almost always a model miss. Snap x to the spec center
            #      (bounds midpoint) when VLM y is valid but x isn't.
            #   3. Hard fallback: bounds_center.
            spec_x = spec_y = None
            if p.get("bounds"):
                spec_x, spec_y = bounds_center(
                    p["bounds"], self.card, (screen_w, screen_h), self.target_density
                )
            vx = vy = None
            if p.get("text"):
                try:
                    vx, vy = self._ground_text(p["text"], screenshot, screen_w, screen_h)
                    logger.info(
                        f"copy_reply: VLM-grounded {p['text']!r} -> ({vx},{vy})"
                    )
                except RuntimeError as e:
                    logger.warning(f"copy_reply: VLM grounding failed: {e}")

            def _in(rng, v):
                return rng is None or (rng[0] <= v <= rng[1])

            vx_ok = vx is not None and _in(p.get("valid_x"), vx)
            vy_ok = vy is not None and _in(p.get("valid_y"), vy)
            if vx_ok and vy_ok:
                x, y = vx, vy
                note = f"VLM ({vx},{vy})"
            elif vy_ok and spec_x is not None:
                # Common Qwen-VL failure mode: correct y, bogus x. Keep y.
                x, y = spec_x, vy
                logger.warning(
                    f"copy_reply: VLM x={vx} outside valid_x={p.get('valid_x')}; "
                    f"snapping x to spec_x={spec_x} (kept VLM y={vy})"
                )
                note = f"VLM-y + spec-x ({spec_x},{vy})"
            elif spec_x is not None and spec_y is not None:
                x, y = spec_x, spec_y
                logger.warning(
                    f"copy_reply: VLM unusable (vx={vx}, vy={vy}); using bounds "
                    f"center ({spec_x},{spec_y})"
                )
                note = f"bounds-center ({spec_x},{spec_y})"
            else:
                logger.warning("copy_reply: no usable locator; skipping tap")
                return JSONAction(action_type="wait"), True, "no copy locator"
            return (
                JSONAction(action_type="click", x=x, y=y),
                True,
                f"tap copy via {note}",
            )

        if kind == "handoff":
            self._maybe_persist_reply()
            reply_note = (
                f"\n\nAgent reply captured:\n{self._last_agent_reply}"
                if self._last_agent_reply
                else ""
            )
            return JSONAction(
                action_type="ask_user",
                text=(
                    f"Handing control back: {p.get('reason', '')}. The in-app "
                    "agent has surfaced the result; please review and confirm "
                    "any irreversible action yourself."
                    f"{reply_note}"
                ),
            ), True, ""

        if kind == "done":
            self._maybe_persist_reply()
            return JSONAction(action_type="finished", goal_status=p.get("status", "complete")), True, ""

        logger.warning(f"Unsupported step kind={kind}; emitting ask_user")
        return JSONAction(
            action_type="ask_user",
            text=f"Card step not supported by adapter: {kind} {p}",
        ), True, ""

    def _ground_text(
        self,
        target: str,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[int, int]:
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _GROUNDING_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Click on the UI element matching: {target!r}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=128,
        )
        if not raw:
            raise RuntimeError(f"Grounding LLM returned empty for {target!r}")

        rx, ry = _extract_xy(raw)
        logger.info(
            f"Grounding {target!r} on {screen_w}x{screen_h}: raw={raw!r} "
            f"-> extracted=({rx},{ry})"
        )
        if rx is None or ry is None:
            raise RuntimeError(f"Grounding model could not find {target!r}")
        # Detect coordinate-system: if either value clearly exceeds the 0-999
        # normalized range, treat as absolute pixels in the source image.
        if rx > 999 or ry > 999:
            px, py = int(rx), int(ry)
        else:
            px, py = int(rx * screen_w / 999), int(ry * screen_h / 999)
        logger.info(f"Grounding {target!r}: mapped to pixel ({px},{py})")
        return px, py

    def _maybe_persist_reply(self) -> None:
        """Dump the captured in-app agent reply as JSON to:
          1. APPCARDS_REPLY_OUT (if set) — for parent processes like FlowRunner;
          2. <MW traj dir>/agent_reply.json — always, so the reply lives next
             to traj.json / screenshots and survives MW's per-run backup of
             traj_logs/user_task/. Best-effort; never raises."""
        payload = json.dumps(
            {
                "reply": self._last_agent_reply,
                "target_app": self.target_app,
            },
            ensure_ascii=False,
        )
        targets: list[Path] = []
        env_path = os.getenv(_REPLY_OUT_ENV)
        if env_path:
            targets.append(Path(env_path))
        # MobileWorld dumps the active run under traj_logs/user_task/ (see
        # CLAUDE.md). Drop the reply there too so it's discoverable by default.
        traj_dir = Path("traj_logs") / "user_task"
        if traj_dir.exists():
            targets.append(traj_dir / "agent_reply.json")
        for path in targets:
            try:
                path.write_text(payload, encoding="utf-8")
                logger.info(
                    f"Persisted captured reply to {path} "
                    f"({len(self._last_agent_reply or '')} chars)"
                )
            except OSError as e:
                logger.warning(f"Failed to persist reply to {path}: {e}")

    def _poll_agent_reply(self, screenshot) -> tuple[bool, str | None]:
        """Ask the VLM whether the in-app assistant has finished replying,
        and capture the reply text. Returns (done, text)."""
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _REPLY_WATCH_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Has the in-app assistant finished its reply?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=600,
        )
        if not raw:
            logger.warning("Reply-watch LLM returned empty; treating as 'not done'")
            return False, None

        m = _JSON_FENCE.search(raw)
        payload = m.group(1) if m else raw
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            import ast

            try:
                data = ast.literal_eval(payload)
            except (ValueError, SyntaxError):
                logger.warning(f"Reply-watch unparseable response: {raw!r}")
                return False, None
        if not isinstance(data, dict):
            return False, None
        done = bool(data.get("done"))
        text = data.get("text")
        if isinstance(text, str):
            text = text.strip() or None
        else:
            text = None
        return done, text
