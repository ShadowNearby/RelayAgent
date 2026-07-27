"""Reply scraping + screen-stability hashing for RelayAgent.

The accessibility-tree-first reply path: `_extract_reply_text_from_dump` scrapes
the assistant's reply text from the uiautomator XML, `_dump_visible_text_hash` /
`_hash_screenshot_region` are the two-stage `wait_for_reply` precheck signals,
and `_stitch_chunks` / `_normalize_for_dedup` merge sliding-window VLM chunks.
The reply-watch / accept-defaults system prompts live here because they belong to
the same reply-handling path. Split out of `relay_agent.py`.
"""

from __future__ import annotations

import hashlib
import os
import re

from loguru import logger

from agents.device import get_backend

_REPLY_WATCH_SYSTEM = (
    "You watch an in-app AI assistant render its reply on a phone screen. "
    "Read the assistant's reply to the user's most recent message off the "
    "screenshot. Ignore UI chrome (input bar, suggestion chips, status bar) "
    "and the user's own message bubbles. "
    "Reply with ONE ```json``` fenced object: "
    '{"text": "<the assistant\'s reply text verbatim, '
    'or null if you cannot read it>"} . '
    "Keep `text` short (<= 500 chars); summarize tail only if too long."
)
_NM_ADVANCE_SYSTEM = (
    "You are driving a phone on behalf of a user whose task is being handled by "
    "an in-app AI assistant. The assistant has replied and may show option cards "
    "(store choices, item specs, quantities). Your job is NOT to make real "
    "choices for the user — only to ACCEPT the assistant's recommended DEFAULTS "
    "and advance the interaction until the screen reaches the final human-confirmation "
    "step, then STOP. Rules, in priority order:\n"
    "1. If ANY visible button would perform an IRREVERSIBLE action — pay, place/"
    "submit the order, confirm payment, confirm a ride or booking (labels like "
    "立即支付, 支付宝付款, 微信支付, 提交订单, 确认支付, 立即下单, 去支付, 确认下单, "
    "立即叫车, 确认叫车, Pay, Place order) — the task MUST stop here for the human. "
    "Set cta_present=true and return NO advance point.\n"
    "2. Otherwise, if a button simply PROCEEDS by accepting the assistant's "
    "recommended/default option (labels like 选这个, 选好了, 确定, 确认, 下一步, "
    "继续, 保存, Confirm, Next), return its center as `advance`.\n"
    "3. Otherwise (the reply is just informational, nothing to advance, or you "
    "are unsure), set done=true and return no advance.\n"
    "When unsure whether a button is irreversible, treat it as irreversible "
    "(prefer stopping over tapping). Reply with ONE ```json``` fenced object: "
    '{"cta_present": <bool>, "cta_label": "<text or null>", '
    '"advance": [<x 0-999>, <y 0-999>] or null, "advance_label": "<text or null>", '
    '"done": <bool>}.'
)


# Common labels for in-app "stop generating" / "thinking" buttons. Used ONLY
# as a chrome-filter for the reply-text scrape (so e.g. "停止生成" doesn't
# leak into the extracted reply text). The done-detection signal is the
# text-hash diff in wait_for_reply Stage 2, not these markers.
_DEFAULT_STREAMING_MARKERS: tuple[str, ...] = (
    "停止生成", "停止回答", "停止", "生成中", "正在生成", "思考中",
    "Stop generating", "Stop", "Generating", "Thinking",
)


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
    backend = get_backend()
    nodes = backend.dump_ui_tree(
        dump_timeout=dump_timeout, pull_timeout=pull_timeout
    )
    if nodes is None:
        return None
    # Drop nodes fully inside the status-bar strip (the clock flips the hash
    # once a minute) AND nodes fully inside the input-area strip (rotating
    # input placeholders / send-button state text flip it every tick, so the
    # stability streak never reaches done and every reply rides the timeout
    # ceiling). Mirrors both crops the pixel-hash precheck already applies.
    try:
        _, screen_h = backend.screen_size()
        top_cutoff, bot_cutoff = _crop_cutoffs(screen_h)
    except Exception:  # size unavailable (e.g. mocked backend) — no crop
        top_cutoff, bot_cutoff = 0, 0
    parts: list[str] = []
    for n in nodes:
        if top_cutoff and n.bounds is not None and n.bounds[3] <= top_cutoff:
            continue
        if bot_cutoff and n.bounds is not None and n.bounds[1] >= bot_cutoff:
            continue
        if n.text:
            parts.append(n.text)
        if n.desc and n.desc != n.text:
            parts.append(n.desc)
    joined = "␟".join(parts)
    return hashlib.blake2b(joined.encode("utf-8", "replace"), digest_size=12).hexdigest()


# Screen crop ratios shared by the reply scrape and the screenshot-region
# hash: strip the status bar (top, default 8%) and the input/keyboard area
# (bottom, default 18%). Device-profile knobs — punch-hole rows, taller nav
# pills or unusual input bars move them via env.
_CROP_TOP_ENV = "RELAY_CROP_TOP"
_CROP_BOTTOM_ENV = "RELAY_CROP_BOTTOM"


def _crop_cutoffs(h: int) -> tuple[int, int]:
    """(top_cutoff, bottom_cutoff) in pixels for screen height `h`: content
    rows above/below them are status bar / input area and get dropped."""
    def _ratio(env: str, default: float) -> float:
        raw = os.getenv(env)
        if raw:
            try:
                return min(0.45, max(0.0, float(raw)))
            except ValueError:
                logger.warning(f"Invalid {env}={raw!r}, using {default}")
        return default

    top = _ratio(_CROP_TOP_ENV, 0.08)
    bottom = _ratio(_CROP_BOTTOM_ENV, 0.18)
    return int(h * top), int(h * (1.0 - bottom))


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
      1. Dump the normalized a11y tree.
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
    tree = get_backend().dump_ui_tree(dump_timeout=3, pull_timeout=2)
    if tree is None:
        return None
    top_cutoff, bot_cutoff = _crop_cutoffs(screen_h)
    # (top_y, text)
    nodes: list[tuple[int, str]] = []
    for n in tree:
        if not n.text or n.center is None:  # center=None ⇒ no/zero-area bounds
            continue
        y1 = n.bounds[1]
        # Drop status bar / input area / off-screen nodes.
        if y1 < top_cutoff or y1 > bot_cutoff:
            continue
        nodes.append((y1, n.text))
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
                # `u in t`: the bubble node contains the typed text (plus
                # timestamps etc.) — always a safe cut. The reverse direction
                # (`t in u`, for bubbles the app truncated/split) needs a
                # length gate: a short reply-area node that happens to be a
                # substring of the request (a category chip like "平板电脑",
                # a price anchor) must not drag cut_y into the reply and
                # delete everything above it. Require the node to cover at
                # least half the typed text (min 6 chars).
                if u in t or (t in u and len(t) >= max(6, len(u) // 2)):
                    cut_y = max(cut_y, y)
    # Filter: above user bubble OR known chrome OR streaming markers.
    excludes = set(_REPLY_CHROME_LABELS) | set(extra_excludes)
    excludes |= set(_DEFAULT_STREAMING_MARKERS)
    candidates: list[tuple[int, str]] = []
    for y, t in nodes:
        if cut_y >= 0 and y <= cut_y:
            continue
        # Exact chrome match, tolerating a trailing ellipsis/dots — streaming
        # buttons often render as "Stop generating…" while the label list
        # carries the bare form.
        if t in excludes or t.rstrip("…. ") in excludes:
            continue
        # Substring-match exclusion for noisy chrome variants ("AI 内容..." etc.)
        # — CJK labels only. English chrome words ("Stop", "Share", "More", …)
        # are common reply vocabulary, so they must exact-match above, never
        # substring-match, or English reply lines get dropped wholesale.
        if any(x and len(x) >= 4 and not x.isascii() and x in t for x in excludes):
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
    top, bot = _crop_cutoffs(h)
    crop = image.crop((0, top, w, bot))
    small = crop.convert("L").resize((48, 96))
    return hashlib.blake2b(small.tobytes(), digest_size=12).hexdigest()


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
    # append its lines unless a PREVIOUS chunk already emitted that line
    # (normalized) — the sliding windows only overlap across chunk seams, so
    # dedup must only look across chunks. Legit repeats WITHIN one chunk
    # (two store cards both showing "人均：¥80") are kept verbatim, matching
    # the single-chunk early return above. Blank lines pass through
    # unconditionally so paragraph breaks survive, but consecutive blanks
    # are collapsed.
    out_lines: list[str] = []
    seen: set[str] = set()  # normalized lines emitted by PREVIOUS chunks only
    new_line_counts: list[int] = []
    for c in chunks:
        added = 0
        chunk_keys: set[str] = set()
        for line in c.splitlines():
            if not line.strip():
                if out_lines and out_lines[-1].strip():
                    out_lines.append("")
                continue
            key = _normalize_for_dedup(line)
            if not key or key in seen:
                continue
            chunk_keys.add(key)
            out_lines.append(line)
            added += 1
        seen |= chunk_keys
        new_line_counts.append(added)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    merged = "\n".join(out_lines)
    logger.info(
        f"_stitch_chunks: merged {len(chunks)} chunks by line-dedup "
        f"(new lines per chunk: {new_line_counts}) -> {len(merged)} chars"
    )
    return merged
