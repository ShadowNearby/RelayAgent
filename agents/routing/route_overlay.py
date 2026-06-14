"""Trace-guided route solidification overlay.

Closes the loop between `leg_judge`'s success/failure verdict and the
three-stage app/capability router (see docs/nl_flow.zh.md §3 and the SkVM
"JIT solidification" idea): a routing decision that the leg judge repeatedly
confirms as `success` is *solidified* into a table lookup, so the next time the
same intent shows up the router returns it with ZERO LLM calls. A route that
starts failing is paused (self-correction) and the router falls back to the
live LLM stages.

This is a *learned, local* artifact (default under `traj_logs/`, which is
git-ignored) — not authoritative. The matrix CSV stays the source of truth;
the overlay only short-circuits selection for intents it has already seen
succeed. Promoting high-confidence entries back into the matrix is a separate
(human-reviewed) step, deferred.

Best-effort by construction: every read/write is wrapped so a corrupt or
unwritable store degrades to "no solidified routes" and NEVER breaks planning
or a flow run (per CLAUDE.md fallback policy).

route_key modes (`RELAY_ROUTE_KEY_MODE`, default `b`):
- "a" (option A): hash of the *normalized synthesized prompt*. Solidifies only
  repeated / near-identical intents, mirroring run_plan's exact-string cache.
- "b" (option B, default): value-independent — hash of the planner's
  *provisional capability* + app hint + request-locale bucket, so "navigate to
  A" and "navigate to B" share one solidified route. Falls back to A whenever
  no provisional capability is available, so it degrades safely.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Verdict statuses, mirrored from agents.flow.leg_judge (kept as literals to avoid a
# hard import cycle; leg_judge does not import this module).
_SUCCESS = "success"
_FAILURE = "failure"


def _default_path() -> Path:
    env = os.getenv("RELAY_ROUTE_OVERLAY_PATH")
    if env:
        return Path(env).expanduser()
    return REPO_ROOT / "traj_logs" / "route_overlay.json"


def _enabled() -> bool:
    return os.getenv("RELAY_ROUTE_OVERLAY", "1") == "1"


def _min_hits() -> int:
    try:
        return max(1, int(os.getenv("RELAY_ROUTE_SOLIDIFY_HITS", "3")))
    except ValueError:
        return 3


def _min_rate() -> float:
    try:
        return float(os.getenv("RELAY_ROUTE_SOLIDIFY_RATE", "0.8"))
    except ValueError:
        return 0.8


def _max_consec_fails() -> int:
    try:
        return max(1, int(os.getenv("RELAY_ROUTE_MAX_FAILS", "2")))
    except ValueError:
        return 2


_WS_RE = re.compile(r"\s+")


def route_key(synthesized_prompt: str) -> str:
    """Stable key for one routing decision (option A: normalized prompt hash).

    Lowercased + whitespace-collapsed so trivial spacing/case differences map
    to the same key; hashed so the stored JSON keys stay compact and don't
    carry the full (possibly long) prompt text.
    """
    norm = _WS_RE.sub(" ", (synthesized_prompt or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _locale_bucket(text: str) -> str:
    """Coarse request-language bucket so navigation in Chinese (→ 高德) and in
    English (→ Gemini) never collapse onto one solidified route.

    Decided by a **majority vote** over the script-bearing characters, not by the
    mere presence of one codepoint — otherwise a lone CJK proper noun in an
    English request (e.g. "navigate to 北京") would flip the whole request to
    `cjk` and risk matching a Chinese-nav solidified route. Ties / no
    script-bearing chars → `latin` (the safe default for the Latin-script apps).
    """
    cjk = latin = 0
    for ch in text or "":
        if (
            "一" <= ch <= "鿿"   # CJK ideographs
            or "぀" <= ch <= "ヿ"  # Hiragana + Katakana
            or "가" <= ch <= "힣"  # Hangul syllables
        ):
            cjk += 1
        elif ch.isalpha():        # Latin (and other alphabetic) letters
            latin += 1
    return "cjk" if cjk > latin else "latin"


def route_key_b(
    provisional_cap: str | None,
    provisional_app: str | None,
    locale_bucket: str,
) -> str | None:
    """Value-independent key (option B): `provisional_cap | app | locale` hash.

    Keyed on the planner's *provisional* capability (+ app hint + request-locale
    bucket) rather than the literal prompt, so "navigate to A" and "navigate to
    B" share one solidified route. Returns None when there is no provisional
    capability to key on — the caller then falls back to option A. A `b:` prefix
    keeps B keys distinct from A keys so both can coexist in one store.
    """
    cap = (provisional_cap or "").strip().lower()
    if not cap:
        return None
    app = (provisional_app or "").strip().lower()
    raw = f"{cap}|{app}|{locale_bucket}"
    return "b:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compute_route_key(
    synthesized_prompt: str,
    *,
    provisional_cap: str | None = None,
    provisional_app: str | None = None,
    mode: str | None = None,
) -> str:
    """Pick the route key per `RELAY_ROUTE_KEY_MODE` (default `b`).

    `b` → value-independent option B when a provisional capability is available,
    else option A. `a` → always option A (value-bearing; zero cross-intent
    reuse). Any provisional gap degrades safely to A.
    """
    mode = (mode or os.getenv("RELAY_ROUTE_KEY_MODE", "b")).strip().lower()
    if mode == "b":
        kb = route_key_b(provisional_cap, provisional_app, _locale_bucket(synthesized_prompt))
        if kb:
            return kb
    return route_key(synthesized_prompt)


def _pair(app: str, cap: str) -> str:
    return f"{app}/{cap}"


class RouteOverlay:
    """Read/lookup/record over the JSON solidification store.

    The store shape (keyed by route_key):
        { "<key>": {
            "intent": "<readable label>",
            "routes": { "<app>/<cap>": {
                "success": int, "failure": int, "loading": int,
                "unknown": int, "consec_fail": int, "last_status": str
            } } } }
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self.enabled = _enabled()

    # --------------------------------------------------------------- io

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.exists() and self.path.stat().st_size > 0:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"route overlay unreadable at {self.path} ({e}); treating as empty")
        return {}

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent,
            prefix=".route_overlay_", suffix=".tmp", delete=False
        )
        tmp = Path(fh.name)
        try:
            with fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # atomic on POSIX; a crash never leaves half-JSON
        except Exception:
            tmp.unlink(missing_ok=True)  # don't leak the temp file on a dump/replace failure
            raise

    # ----------------------------------------------------------- lookup

    def lookup(self, key: str) -> tuple[str, str] | None:
        """Return a solidified (app_id, capability_id) for `key`, or None.

        A route is solidified when it has ≥`min_hits` successes, a success
        rate ≥`min_rate`, and fewer than `max_consec_fails` consecutive recent
        failures. None means "fall back to the live LLM stages".
        """
        if not self.enabled or not key:
            return None
        try:
            entry = self._load().get(key)
            if not isinstance(entry, dict):
                return None
            routes = entry.get("routes") or {}
            min_hits, min_rate, max_fails = _min_hits(), _min_rate(), _max_consec_fails()
            best: tuple[str, str] | None = None
            best_succ = -1
            for pair, stats in routes.items():
                if not isinstance(stats, dict):
                    continue
                succ = int(stats.get("success", 0))
                fail = int(stats.get("failure", 0))
                consec = int(stats.get("consec_fail", 0))
                if succ < min_hits or consec >= max_fails:
                    continue
                if (succ + fail) and succ / (succ + fail) < min_rate:
                    continue
                if succ > best_succ and "/" in pair:
                    best_succ = succ
                    app, cap = pair.split("/", 1)
                    best = (app, cap)
            return best
        except Exception as e:  # never let a lookup break routing
            logger.warning(f"route overlay lookup failed for {key!r}: {e}")
            return None

    # ----------------------------------------------------------- record

    def record(self, key: str, intent: str, app: str, cap: str, status: str) -> None:
        """Fold one leg's verdict into the store. Never raises.

        `success` resets the consecutive-fail counter; `failure` increments it
        (this is the self-correction: enough consecutive failures pause the
        route's solidification). `loading`/`unknown` are recorded but neutral.
        """
        if not self.enabled or not key or not app or not cap:
            return
        try:
            data = self._load()
            entry = data.setdefault(key, {"intent": intent, "routes": {}})
            if intent:
                entry["intent"] = intent  # keep the latest readable label
            routes = entry.setdefault("routes", {})
            stats = routes.setdefault(
                _pair(app, cap),
                {"success": 0, "failure": 0, "loading": 0, "unknown": 0,
                 "consec_fail": 0, "last_status": ""},
            )
            stats[status] = int(stats.get(status, 0)) + 1
            if status == _SUCCESS:
                stats["consec_fail"] = 0
            elif status == _FAILURE:
                stats["consec_fail"] = int(stats.get("consec_fail", 0)) + 1
            stats["last_status"] = status
            self._atomic_write(data)
            logger.info(
                f"route overlay recorded {app}/{cap} [{status}] for {key} "
                f"(success={stats['success']} failure={stats['failure']} "
                f"consec_fail={stats['consec_fail']})"
            )
        except Exception as e:  # advisory store; a write miss must not break the flow
            logger.warning(f"route overlay record failed for {key!r}: {e}")
