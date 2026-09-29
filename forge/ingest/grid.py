"""Overlay a labelled coordinate grid on an image before the model reads it.

Every region in a specification - the crop shown beside a question, and the
artwork printed onto a face - is given as fractions of the image. Asked for
those by eye, a vision model is confidently and consistently wrong: it is very
good at "the talker is the white strip under the second shelf" and quite bad at
"that is at y = 0.42". On a display stand its shelf talkers came back at
y = 0.224, which is up in the poster, so the model's own face was printed onto
all four of them.

Tightening the instruction did not help, because the instruction was never the
problem - estimating a fraction from a picture is. So the picture now carries
the answer: a grid at every 0.05 with the tenths labelled down both sides and
across the top and bottom. Reading a coordinate off a printed label is a
different task from estimating one, and it is a task this kind of model is
reliable at.

The overlay goes only on the copy the model looks at. Crops are always cut from
the original bytes, so no gridline ever ends up printed on a model.
"""

from __future__ import annotations

import io

# Every 0.05 gets a line; every 0.1 gets a heavier line and a number. Finer than
# this and the lines start to obscure what is being measured, which defeats the
# purpose - the model still has to see the object.
MINOR = 0.05
MAJOR = 0.10

# Drawn in a colour nothing in a product photograph is, so a gridline is never
# mistaken for an edge of the object.
LINE = (255, 0, 255)
LABEL_BG = (255, 0, 255)
LABEL_FG = (255, 255, 255)

# Faint enough to read the picture through, strong enough to follow across it.
MINOR_ALPHA = 60
MAJOR_ALPHA = 130


def annotate(data: bytes) -> bytes:
    """Return the image with a labelled 0-1 grid drawn over it.

    Returns the original bytes unchanged if anything goes wrong: a missing
    grid costs some accuracy, a failed extraction costs the whole run.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except ImportError:
        return data

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return data

    width, height = image.size
    if width < 60 or height < 60:
        return data

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Label size follows the image, so a grid is as readable on a 4000px
    # photograph as on an 800px one.
    step = max(9, min(26, int(min(width, height) * 0.022)))

    def line(x0, y0, x1, y1, major):
        draw.line([(x0, y0), (x1, y1)],
                  fill=LINE + (MAJOR_ALPHA if major else MINOR_ALPHA,),
                  width=2 if major else 1)

    count = int(round(1.0 / MINOR))
    for i in range(count + 1):
        fraction = i * MINOR
        major = abs((fraction / MAJOR) - round(fraction / MAJOR)) < 1e-6

        x = int(fraction * (width - 1))
        y = int(fraction * (height - 1))
        line(x, 0, x, height, major)
        line(0, y, width, y, major)

        if not major:
            continue

        text = f"{fraction:.1f}"
        # Along the top and the bottom for x, down both sides for y, so a
        # coordinate can be read without tracking a line across the whole
        # picture - which is where the error creeps in.
        for tx, ty in ((x + 3, 2), (x + 3, height - step - 3)):
            _label(draw, tx, ty, text, step)
        for tx, ty in ((2, y + 2), (width - step * 2 - 4, y + 2)):
            _label(draw, tx, ty, text, step)

    return _encode(Image.alpha_composite(image.convert("RGBA"), overlay))


def _label(draw, x: int, y: int, text: str, step: int) -> None:
    """A number on a solid plate, so it is legible over any background."""
    pad = 2
    box_w = step * 2
    draw.rectangle([x - pad, y - pad, x + box_w, y + step],
                   fill=LABEL_BG + (210,))
    try:
        from PIL import ImageFont  # noqa: PLC0415
        font = ImageFont.load_default(size=step)
    except Exception:  # noqa: BLE001
        font = None
    draw.text((x, y), text, fill=LABEL_FG, font=font)


def _encode(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
