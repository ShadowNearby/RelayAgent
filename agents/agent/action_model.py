"""Action data model — the wire format predict() returns and the runtime executes.

The `JSONAction` model and the action-type string constants. The agent and the
native runtime rely on `action.model_dump(exclude_none=True)`,
`action.action_json`, the validators and `__eq__`, so those are kept stable.

Pure Python (no pydantic): the Android build embeds CPython via Chaquopy,
which has no pydantic-core wheel, and this was the only pydantic consumer in
the runtime import chain. Behavior is pinned by tests/test_action_model.py,
written against the original pydantic implementation.
"""
from __future__ import annotations

from typing import Any

# Action type constants (string values are the action wire format).
ANSWER = "answer"
CLICK = "click"
DOUBLE_TAP = "double_tap"
FINISHED = "finished"
INPUT_TEXT = "input_text"
KEYBOARD_ENTER = "keyboard_enter"
LONG_PRESS = "long_press"
NAVIGATE_BACK = "navigate_back"
NAVIGATE_HOME = "navigate_home"
OPEN_APP = "open_app"
SCROLL = "scroll"
STATUS = "status"
SWIPE = "swipe"
UNKNOWN = "unknown"
WAIT = "wait"
DRAG = "drag"
ASK_USER = "ask_user"
MCP = "mcp"
ENV_FAIL = "error_env"

_ACTION_TYPES = (
    CLICK,
    DOUBLE_TAP,
    SCROLL,
    SWIPE,
    INPUT_TEXT,
    NAVIGATE_HOME,
    NAVIGATE_BACK,
    KEYBOARD_ENTER,
    OPEN_APP,
    STATUS,
    WAIT,
    LONG_PRESS,
    ANSWER,
    FINISHED,
    UNKNOWN,
    DRAG,
    ASK_USER,
    MCP,
)

_SCROLL_DIRECTIONS = ("left", "right", "down", "up")

# Declaration order matters: model_dump emits fields in this order.
_FIELDS = (
    "action_type",
    "index",
    "x",
    "y",
    "text",
    "direction",
    "goal_status",
    "app_name",
    "keycode",
    "start_x",
    "start_y",
    "end_x",
    "end_y",
    "action_name",
    "action_json",
)


class JSONAction:
    """A parsed action emitted by an agent's predict().

    Example:
        JSONAction(**{'action_type': 'click', 'x': 100, 'y': 200})

    Unknown keyword arguments are ignored (the LLM wire format can carry
    stray keys), matching pydantic v2's default extra="ignore".
    """

    action_type: str | None
    index: int | None
    x: int | None
    y: int | None
    text: str | None
    direction: str | None
    goal_status: str | None
    app_name: str | None
    keycode: str | None
    start_x: int | None
    start_y: int | None
    end_x: int | None
    end_y: int | None
    action_name: str | None
    action_json: dict | None

    def __init__(self, **kwargs: Any) -> None:
        for f in _FIELDS:
            setattr(self, f, kwargs.get(f))

        if self.action_type is not None and self.action_type not in _ACTION_TYPES:
            raise ValueError(f"Invalid action type: {self.action_type}")
        if self.index is not None:
            try:
                self.index = int(self.index)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid index: {self.index}") from None
        if self.x is not None:
            self.x = round(self.x)
        if self.y is not None:
            self.y = round(self.y)
        if self.direction is not None and self.direction not in _SCROLL_DIRECTIONS:
            raise ValueError(f"Invalid scroll direction: {self.direction}")
        if self.text is not None and not isinstance(self.text, str):
            self.text = str(self.text)
        if self.keycode is not None and not self.keycode.startswith("KEYCODE_"):
            raise ValueError(f"Invalid keycode: {self.keycode}")
        if self.index is not None and (self.x is not None or self.y is not None):
            raise ValueError("Either an index or a <x, y> should be provided.")

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        if exclude_none:
            return {f: v for f in _FIELDS if (v := getattr(self, f)) is not None}
        return {f: getattr(self, f) for f in _FIELDS}

    def __repr__(self) -> str:
        kv = ", ".join(f"{k}={v!r}" for k, v in self.model_dump(exclude_none=True).items())
        return f"JSONAction({kv})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, JSONAction):
            return False
        return _compare_actions(self, other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)


def _compare_actions(a: JSONAction, b: JSONAction) -> bool:
    """Compare two JSONActions (case-insensitive for app_name/text)."""
    if a.app_name is not None and b.app_name is not None:
        app_name_match = a.app_name.lower() == b.app_name.lower()
    else:
        app_name_match = a.app_name == b.app_name

    if a.text is not None and b.text is not None:
        text_match = a.text.lower() == b.text.lower()
    else:
        text_match = a.text == b.text

    return (
        app_name_match
        and text_match
        and a.action_type == b.action_type
        and a.index == b.index
        and a.x == b.x
        and a.y == b.y
        and a.keycode == b.keycode
        and a.direction == b.direction
        and a.goal_status == b.goal_status
        and a.start_x == b.start_x
        and a.start_y == b.start_y
        and a.end_x == b.end_x
        and a.end_y == b.end_y
    )
