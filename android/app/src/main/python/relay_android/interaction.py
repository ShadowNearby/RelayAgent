"""OverlayInteraction — the Android InteractionProvider.

Maps the runtime's interaction hooks (agents/interaction.py) onto the
overlay UI: ask_user blocks on the floating panel (回答 -> answer string,
我来接管 -> None = handoff terminal, same semantics as terminal EOF);
emit_status drives the status chip; should_stop polls the Stop button.
"""
from __future__ import annotations

import json

from java import jclass

from agents.interaction import InteractionProvider

Bridge = jclass("com.relayagent.app.DeviceBridge")


class OverlayInteraction(InteractionProvider):
    def ask_user(self, text: str | None, input_prompt: str = "> ") -> str | None:
        answer = Bridge.askUser(text or "")
        return str(answer) if answer is not None else None

    def emit_status(self, event: dict) -> None:
        try:
            Bridge.emitStatus(json.dumps(event, ensure_ascii=False))
        except Exception:  # status is fire-and-forget; never break the loop
            pass

    def should_stop(self) -> bool:
        return bool(Bridge.shouldStop())
