"""Tiny image helpers. `pil_to_base64` is ported from MobileWorld's
`agents/utils/helpers.py` so RelayAgent's grounding / reply-watch payloads no
longer import `mobile_world`."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image


def pil_to_base64(image) -> str:
    """Convert a PIL image (or raw PNG bytes) to a base64 PNG string."""
    if not isinstance(image, Image.Image):
        image = Image.open(BytesIO(image)).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
