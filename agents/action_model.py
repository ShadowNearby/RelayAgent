"""Action data model — the wire format predict() returns and the runtime executes.

Ported from MobileWorld's `runtime/utils/models.py` (the `JSONAction` model and
the action-type string constants) so RelayAgent no longer imports `mobile_world`.
Kept field-for-field identical — `action.model_dump(exclude_none=True)`,
`action.action_json`, the validators and `__eq__` — so the agent and the native
runtime behave exactly as they did on the mw `JSONAction`.

Only the pieces the agent path actually uses are kept; mw's FastAPI request
models, Docker models and the giant package↔label maps are intentionally dropped.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator

# Action type constants (string values match mw's wire format byte-for-byte).
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


class JSONAction(BaseModel):
    """A parsed action emitted by an agent's predict().

    Example:
        JSONAction(**{'action_type': 'click', 'x': 100, 'y': 200})
    """

    action_type: str | None = None
    index: str | int | None = None
    x: int | None = None
    y: int | None = None
    text: str | None = None
    direction: str | None = None
    goal_status: str | None = None
    app_name: str | None = None
    keycode: str | None = None
    clear_text: bool | None = None
    start_x: int | None = None
    start_y: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    action_name: str | None = None
    action_json: dict | None = None

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, v: str | None) -> str | None:
        if v is not None and v not in _ACTION_TYPES:
            raise ValueError(f"Invalid action type: {v}")
        return v

    @field_validator("index")
    @classmethod
    def validate_index(cls, v: str | int | None) -> int | None:
        if v is not None:
            return int(v)
        return v

    @field_validator("x", "y", mode="before")
    @classmethod
    def validate_coordinates(cls, v: int | float | None) -> int | None:
        if v is not None:
            return round(v)
        return v

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str | None) -> str | None:
        if v is not None and v not in _SCROLL_DIRECTIONS:
            raise ValueError(f"Invalid scroll direction: {v}")
        return v

    @field_validator("text", mode="before")
    @classmethod
    def validate_text(cls, v: Any) -> str | None:
        if v is not None and not isinstance(v, str):
            return str(v)
        return v

    @field_validator("keycode")
    @classmethod
    def validate_keycode(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("KEYCODE_"):
            raise ValueError(f"Invalid keycode: {v}")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.index is not None:
            if self.x is not None or self.y is not None:
                raise ValueError("Either an index or a <x, y> should be provided.")

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
