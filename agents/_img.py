"""Tiny image helpers. `pil_to_base64` builds the grounding / reply-watch
payloads RelayAgent sends to the VLM."""
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
