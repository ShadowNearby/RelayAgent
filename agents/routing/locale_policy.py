"""Locale-driven language policy for in-app agent prompts."""

from __future__ import annotations

import re
from typing import Any


def first_locale(meta: dict[str, Any] | None) -> str | None:
    """Return the first BCP-47 locale declared by a card/catalog entry."""
    raw = (meta or {}).get("locale")
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def language_label(locale: str | None) -> str:
    """Human-readable language target for prompt instructions."""
    tag = (locale or "").strip()
    base = tag.split("-", 1)[0].lower()
    if base == "zh":
        if tag.lower() in {"zh-tw", "zh-hk", "zh-mo", "zh-hant"}:
            return "Traditional Chinese"
        return "Simplified Chinese"
    if base == "en":
        return "English"
    if base == "ja":
        return "Japanese"
    if base == "ko":
        return "Korean"
    if base == "fr":
        return "French"
    if base == "de":
        return "German"
    if base == "es":
        return "Spanish"
    return tag or "the app's primary locale language"


def locale_policy_text(locale: str | None = None) -> str:
    target = language_label(locale)
    if locale:
        first = f"first locale `{locale}` ({target})"
    else:
        first = "the app's first locale"
    return (
        "Locale policy for the text typed into the in-app agent: default to "
        f"{first}. If the user's request or the prompt explicitly asks for a "
        "different language, honor that explicit instruction. Preserve proper "
        "nouns, addresses, product names, code, URLs, emails, ids, and quoted "
        "literal text in their original language."
    )


_EXPLICIT_LANGUAGE_PATTERNS = [
    r"\b(in|into|to|using|with)\s+"
    r"(english|chinese|mandarin|cantonese|japanese|korean|french|german|spanish)\b",
    r"\b(respond|answer|reply|write|summari[sz]e|translate|output)\b"
    r".{0,32}\b(english|chinese|japanese|korean|french|german|spanish)\b",
    r"\b(英文|英语|中文|汉语|普通话|粤语|日文|日语|韩文|韩语|法文|法语|德文|德语|西班牙文|西班牙语)"
    r"(回答|回复|输出|写|撰写|总结|翻译|表达|润色)",
    r"(用|以|使用).{0,8}"
    r"(英文|英语|中文|汉语|普通话|粤语|日文|日语|韩文|韩语|法文|法语|德文|德语|西班牙文|西班牙语)",
]

_EXPLICIT_LANGUAGE_RE = re.compile("|".join(_EXPLICIT_LANGUAGE_PATTERNS), re.I)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def has_explicit_language_instruction(text: str) -> bool:
    """Best-effort guard for prompts that intentionally specify language."""
    return bool(_EXPLICIT_LANGUAGE_RE.search(text or ""))


def appears_compatible_with_locale(text: str, locale: str | None) -> bool:
    """Cheap script check to avoid unnecessary prompt-localization LLM calls."""
    body = text or ""
    base = (locale or "").split("-", 1)[0].lower()
    if not body.strip() or not base:
        return True
    if base == "zh":
        return bool(_CJK_RE.search(body))
    if base == "en":
        return not bool(_CJK_RE.search(body)) and bool(_LATIN_WORD_RE.search(body))
    return True
