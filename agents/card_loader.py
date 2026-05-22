"""Card loading + x_bounds device remapping.

Shared by examples/match_intent.py and the MobileWorld adapter so the
manifest schema has exactly one reader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "manifests"


def load_all_cards(manifests_dir: Path | None = None) -> list[dict[str, Any]]:
    d = manifests_dir or MANIFESTS_DIR
    cards = []
    for path in sorted(d.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            cards.append(yaml.safe_load(f))
    return cards


def load_card_by_app_id(app_id: str, manifests_dir: Path | None = None) -> dict[str, Any]:
    for card in load_all_cards(manifests_dir):
        if card.get("app_id") == app_id:
            return card
    raise FileNotFoundError(f"No manifest found for app_id={app_id!r} under {manifests_dir or MANIFESTS_DIR}")


def _px_to_dp(px: int, dpi: int) -> float:
    return px * 160.0 / dpi


def _dp_to_px(dp: float, dpi: int) -> int:
    return int(round(dp * dpi / 160.0))


def remap_bounds(
    bounds: dict,
    src_metrics: dict,
    tgt_resolution: tuple[int, int],
    tgt_density: int,
) -> list[int]:
    """Remap an x_bounds box from the verified device to the target device.

    Anchor-aware: edge-anchored boxes preserve dp margins from named edges;
    center/none fall back to bi-axial linear scaling.
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

    def edge_anchor(left, top, right, bottom):
        if left is not None:
            x1_t = _dp_to_px(_px_to_dp(left, src_dpi), tgt_density)
            x2_t = x1_t + w_px
        else:
            x2_t = tgt_w - _dp_to_px(_px_to_dp(right, src_dpi), tgt_density)
            x1_t = x2_t - w_px
        if top is not None:
            y1_t = _dp_to_px(_px_to_dp(top, src_dpi), tgt_density)
            y2_t = y1_t + h_px
        else:
            y2_t = tgt_h - _dp_to_px(_px_to_dp(bottom, src_dpi), tgt_density)
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

    sx = tgt_w / src_w
    sy = tgt_h / src_h
    return [int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)]


def bounds_center(
    bounds: dict,
    card: dict,
    tgt_resolution: tuple[int, int],
    tgt_density: int | None,
) -> tuple[int, int]:
    """Return the absolute (x, y) center pixel of an x_bounds selector on the
    target device. Falls back to raw scaling if `provenance.x_device_metrics`
    or `tgt_density` is missing."""
    src = (card.get("provenance") or {}).get("x_device_metrics")
    if src and tgt_density is not None:
        box = remap_bounds(bounds, src, tgt_resolution, tgt_density)
    else:
        box = bounds["box"]
    x1, y1, x2, y2 = box
    return (x1 + x2) // 2, (y1 + y2) // 2
