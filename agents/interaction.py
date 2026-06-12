"""User-interaction provider — ask_user / status events / stop requests.

The runtime historically talked to the user through terminal `input()` /
`print()` in two places: the in-task ASK_USER handoff
(`native_runtime.run_task`) and flow-level ask_user steps
(`flow_runner._run_ask_user`). Both now route through a process-wide
`InteractionProvider`, so the Android app can swap in an overlay-dialog
implementation (and a Stop button / status chip) without touching the loop
logic.

Contract:

- `ask_user(text, input_prompt="> ")` shows `text` (may be multi-line) and
  returns the user's raw answer, or **None on EOF / take-over** — callers
  keep today's semantics: the in-task handoff treats None as the documented
  SUCCESS terminal (stdin redirected under batch runs), flow ask_user treats
  it as the empty default answer.
- `emit_status(event)` is fire-and-forget telemetry (a dict with at least
  `event`); the default ignores it, the Android overlay renders it.
- `should_stop()` is polled at loop boundaries (each run_task step, each
  flow leg). Default False; the Android Stop button flips it.
"""
from __future__ import annotations


class InteractionProvider:
    def ask_user(self, text: str | None, input_prompt: str = "> ") -> str | None:
        raise NotImplementedError

    def emit_status(self, event: dict) -> None:  # noqa: B027 — optional hook
        pass

    def should_stop(self) -> bool:
        return False


class TerminalInteraction(InteractionProvider):
    """The host default: print + input, EOF → None.

    Output bytes match the pre-refactor inline code exactly (print appends
    the newline that the old single f-string prompts carried)."""

    def ask_user(self, text: str | None, input_prompt: str = "> ") -> str | None:
        if text is not None:  # "" still prints its newline (pre-refactor parity)
            print(text, flush=True)
        try:
            return input(input_prompt)
        except EOFError:
            return None


_interaction: InteractionProvider | None = None


def get_interaction() -> InteractionProvider:
    global _interaction
    if _interaction is None:
        _interaction = TerminalInteraction()
    return _interaction


def set_interaction(provider: InteractionProvider) -> None:
    global _interaction
    _interaction = provider
