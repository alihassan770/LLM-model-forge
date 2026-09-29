"""Crop a region of a source image, so a question can show the part it asks about.

Regions arrive as fractions of the image, not pixels, so a crop keeps pointing
at the right place if the source is ever resized.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Crops are padded outward before cutting. A box drawn tightly around a table
# leg, shown on its own, is an unrecognisable grey line - a little context is
# what makes it identifiable.
PAD = 0.06
MIN_FRACTION = 0.12
MAX_EDGE = 420


def crop_region(data: bytes, x: float, y: float, w: float, h: float) -> bytes | None:
    """Return a PNG of the requested region, or None if it cannot be made."""
    try:
        from PIL import Image
    except ImportError:
        # Loud, because the symptom is not. Returning None here produces a 404,
        # which the browser draws as a broken image and nothing writes down.
        # The crops were missing in production for exactly this reason and the
        # logs were clean. Pillow is a declared dependency; if it is absent the
        # environment is wrong and that is worth an error line.
        log.error("Pillow is not installed, so question crops cannot be made. "
                  "It is a declared dependency - reinstall the application.")
        return None

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return None

    width, height = image.size
    if width < 8 or height < 8:
        return None

    # Grow anything too small to be legible, keeping it centred on the region
    # the model actually pointed at.
    if w < MIN_FRACTION:
        x -= (MIN_FRACTION - w) / 2
        w = MIN_FRACTION
    if h < MIN_FRACTION:
        y -= (MIN_FRACTION - h) / 2
        h = MIN_FRACTION

    left = max(0.0, x - PAD)
    top = max(0.0, y - PAD)
    right = min(1.0, x + w + PAD)
    bottom = min(1.0, y + h + PAD)

    box = (int(left * width), int(top * height),
           int(right * width), int(bottom * height))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return None

    try:
        cropped = image.crop(box)
        if cropped.mode not in ("RGB", "L"):
            cropped = cropped.convert("RGB")
        cropped.thumbnail((MAX_EDGE, MAX_EDGE))
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        return None
