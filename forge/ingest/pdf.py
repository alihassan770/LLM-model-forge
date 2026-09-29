"""Reading a PDF: its text layer, and pictures of the pages that matter.

A spec sheet is the best input this app can get. Unlike a photograph, it
usually *states* its dimensions, in a text layer we can extract verbatim - "Overall width: 1520 mm" rather than a number the model had to read off a
drawing. That is why `pdf_text_layer` outranks `image_label` in the spec's
provenance ordering: one is a copy, the other is an interpretation.

So a PDF is read twice, and both readings are sent to the model:

  1. the text layer, exact, per page
  2. a raster of each page it needs to look at, for the drawing itself

Both matter. Text alone cannot say which leg a 70mm refers to; a picture
alone turns an exact 1520 into a reading of a blurry label.

Poppler's command line tools do the work. They are already installed in the
worker image, they read from stdin and write to stdout so nothing touches
disk, and they are far better at broken real-world PDFs than anything that
would fit in this file. Each call is given a timeout: a malformed PDF that
makes a parser spin must not take a request with it.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Poppler is fast, but a 600-page catalogue rendered at 200dpi is not. Every
# call is bounded, and the bound is per page rather than per document.
INFO_TIMEOUT = 20
TEXT_TIMEOUT = 30
RENDER_TIMEOUT = 60

# What the vision models accept before they downscale for us. Rendering above
# this wastes tokens on detail the model will never see.
MAX_EDGE = 1568

# Rendered above the target and downscaled, rather than rendered at it: the
# dimension text on a drawing is small, and resampling a larger raster keeps
# it legible where rendering straight to size does not.
RENDER_DPI = 200
MIN_DPI = 72


class PdfError(Exception):
    """Something the person who uploaded the file can understand."""


@dataclass(frozen=True)
class PdfInfo:
    pages: int
    width_pt: float
    height_pt: float
    encrypted: bool
    title: str = ""

    @property
    def landscape(self) -> bool:
        return self.width_pt > self.height_pt

    @property
    def page_label(self) -> str:
        """The page size in millimetres, which is how a drawing is described."""
        return (f"{self.width_pt * 25.4 / 72:.0f} x "
                f"{self.height_pt * 25.4 / 72:.0f} mm")


@dataclass
class Page:
    number: int                    # 1-based, as the user would say it
    text: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    image: bytes | None = None     # PNG, once rendered

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass
class PdfRead:
    info: PdfInfo
    pages: list[Page]              # only the pages that were selected
    total_pages: int
    skipped: list[int] = field(default_factory=list)

    @property
    def text_layer(self) -> str:
        """Every selected page's text, labelled, ready to put in a prompt."""
        blocks = []
        for p in self.pages:
            if p.has_text:
                blocks.append(f"--- page {p.number} ---\n{p.text.strip()}")
        return "\n\n".join(blocks)

    @property
    def images(self) -> list[tuple[bytes, str]]:
        return [(p.image, "image/png") for p in self.pages if p.image]

    @property
    def scanned(self) -> bool:
        """No text layer anywhere - a scan or an exported drawing."""
        return not any(p.has_text for p in self.pages)


# --------------------------------------------------------------------------- #
# Running poppler
# --------------------------------------------------------------------------- #

def _run(args: list[str], data: bytes, timeout: int, what: str) -> bytes:
    try:
        done = subprocess.run(
            args, input=data, capture_output=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        # Worth naming the package: this is an install problem, not a bad file.
        raise PdfError(
            f"Cannot read PDFs - {args[0]} is not installed on the server "
            f"(poppler-utils)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfError(
            f"That PDF took too long to {what} and was stopped after "
            f"{timeout} seconds. It may be unusually complex or damaged."
        ) from exc

    if done.returncode != 0 and not done.stdout:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()
        detail = detail.splitlines()[-1] if detail else "no detail given"
        raise PdfError(f"That PDF could not be read ({detail}).")
    return done.stdout


def info(data: bytes) -> PdfInfo:
    """Page count, page size and whether the file is encrypted."""
    out = _run(["pdfinfo", "-"], data, INFO_TIMEOUT, "inspect")
    text = out.decode("utf-8", "replace")

    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if value:
            fields[key.strip().lower()] = value.strip()

    try:
        pages = int(fields.get("pages", "0"))
    except ValueError:
        pages = 0
    if pages < 1:
        raise PdfError("That PDF appears to have no pages.")

    width = height = 0.0
    size = re.match(r"([\d.]+)\s*x\s*([\d.]+)\s*pts", fields.get("page size", ""))
    if size:
        width, height = float(size.group(1)), float(size.group(2))

    encrypted = fields.get("encrypted", "no").lower().startswith("yes")
    if encrypted and "print:yes" not in fields.get("encrypted", "").replace(" ", ""):
        # Poppler will often still render a permissions-encrypted file; it
        # cannot open a password-protected one. Try, and fail later if it
        # genuinely cannot, rather than refusing something readable here.
        pass

    return PdfInfo(pages=pages, width_pt=width, height_pt=height,
                   encrypted=encrypted, title=fields.get("title", ""))


def page_text(data: bytes, number: int) -> str:
    """One page's text layer, with its visual layout preserved.

    `-layout` matters more than it looks: a dimension table read without it
    comes out as a column of orphaned numbers, and "1520" on its own line
    tells the model nothing about what it measures.
    """
    out = _run(
        ["pdftotext", "-layout", "-f", str(number), "-l", str(number), "-", "-"],
        data, TEXT_TIMEOUT, "read text from",
    )
    return out.decode("utf-8", "replace")


def render_page(data: bytes, number: int, dpi: int = RENDER_DPI,
                max_edge: int = MAX_EDGE) -> bytes:
    """One page as a PNG, no larger than `max_edge` on its long side."""
    dpi = max(MIN_DPI, int(dpi))
    out = _run(
        ["pdftoppm", "-png", "-r", str(dpi),
         "-f", str(number), "-l", str(number), "-"],
        data, RENDER_TIMEOUT, "render",
    )
    if not out:
        raise PdfError(f"Page {number} of that PDF could not be rendered.")
    return _downscale(out, max_edge)


def _downscale(png: bytes, max_edge: int) -> bytes:
    """Fit a PNG inside max_edge, or return it unchanged if already small."""
    try:
        from PIL import Image
    except ImportError:
        # Returning the page at full size still works, but it costs real money
        # - a 200dpi A4 page is several times the tokens of a downscaled one -
        # so this degrades quietly in the direction of a larger bill. Say so.
        log.error("Pillow is not installed, so PDF pages are sent to the "
                  "model at full resolution. It is a declared dependency - "
                  "reinstall the application.")
        return png

    try:
        image = Image.open(io.BytesIO(png))
        image.load()
        if max(image.size) <= max_edge:
            return png
        # LANCZOS rather than the default: this is line art and small text,
        # which a box filter turns to mush.
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        # A page we cannot resize is still a page we can send.
        return png


# --------------------------------------------------------------------------- #
# Deciding which pages to read
# --------------------------------------------------------------------------- #

# A dimension on a spec sheet looks like one of these. The patterns are
# deliberately loose - they are used to rank pages, not to parse values, so a
# false positive costs a little attention and a false negative costs a page.
_DIMENSION = re.compile(
    r"""
      \d+(?:[.,]\d+)?\s?(?:mm|cm|millimet|centimet)   # 1520 mm
    | \d+(?:[.,]\d+)?\s?(?:m|in|inch|inches|")(?![a-z]) # 30 in, 12"
    | \d+(?:[.,]\d+)?\s?['’]\s?\d*                    # 5' 6
    | \d+\s?[x×]\s?\d+                                # 70 x 70
    | \b\d{3,4}\b                                     # a bare 1520
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Words that mean this page is about the shape of the thing.
_MEASURE_WORDS = (
    "dimension", "width", "height", "depth", "length", "thickness",
    "diameter", "seat", "overall", "spec", "drawing", "elevation", "plan",
    "section", "assembly", "part", "component", "material", "finish",
    "scale", "mm", "cm",
)

# Words that mean it is not.
_NOISE_WORDS = (
    "warranty", "terms and conditions", "privacy", "returns policy",
    "shipping", "invoice", "purchase order", "care instructions",
    "cleaning", "guarantee", "copyright", "trademark", "index",
)


def score_page(text: str) -> tuple[float, list[str]]:
    """How likely this page is to describe the object's geometry."""
    lowered = text.lower()
    reasons: list[str] = []
    score = 0.0

    dimensions = len(_DIMENSION.findall(text))
    if dimensions:
        # Sub-linear: thirty numbers is not three times as informative as ten,
        # and a parts index is mostly numbers.
        score += min(dimensions, 40) ** 0.6
        reasons.append(f"{dimensions} dimension-like values")

    words = [w for w in _MEASURE_WORDS if w in lowered]
    if words:
        score += 1.4 * len(words)
        reasons.append(f"mentions {', '.join(words[:5])}")

    noise = [w for w in _NOISE_WORDS if w in lowered]
    if noise:
        score -= 3.0 * len(noise)
        reasons.append(f"looks like {noise[0]}")

    # A page with no text at all is a scan or a pure drawing - exactly the
    # thing we need to *look* at. It scores modestly rather than zero, so it
    # beats a page of legal text but loses to a real dimension table.
    if not text.strip():
        score += 2.0
        reasons.append("no text layer - likely a drawing or scan")

    return score, reasons


def select_pages(data: bytes, total: int, limit: int) -> list[Page]:
    """Choose which pages are worth rendering, and return them in page order.

    A furniture PDF is usually one to four pages and all of it matters. A
    supplier catalogue is six hundred pages of which two do. Reading the text
    layer is cheap enough to do for every page, so the choice is made on what
    the pages actually say rather than on their position.
    """
    pages: list[Page] = []
    for number in range(1, total + 1):
        try:
            text = page_text(data, number)
        except PdfError:
            # One unreadable page should not lose the document.
            text = ""
        score, reasons = score_page(text)
        pages.append(Page(number=number, text=text, score=score, reasons=reasons))

    # Pages that score negative are warranties, returns policies and terms - # they scored below zero because they say so. Dropping them even when
    # there is room spares the tokens and, more to the point, spares the model
    # a page of prose to weigh against the drawing. Only ever dropped when
    # something better remains.
    useful = [p for p in pages if p.score > 0]
    if useful and len(useful) < len(pages):
        pages = useful

    if len(pages) <= limit:
        return pages

    # Highest scoring first to choose, then back into reading order: a model
    # shown page 9 before page 2 has to work out the ordering itself.
    chosen = sorted(pages, key=lambda p: (-p.score, p.number))[:limit]
    return sorted(chosen, key=lambda p: p.number)


# --------------------------------------------------------------------------- #
# The one function the rest of the app calls
# --------------------------------------------------------------------------- #

def read(data: bytes, max_pages: int = 4, max_edge: int = MAX_EDGE,
         dpi: int = RENDER_DPI) -> PdfRead:
    """Read a PDF into text and page images, ready for the vision client."""
    meta = info(data)

    selected = select_pages(data, meta.pages, max_pages)
    for page in selected:
        try:
            page.image = render_page(data, page.number, dpi=dpi, max_edge=max_edge)
        except PdfError:
            # Keep the text: an exact "1520 mm" is worth more than the picture
            # it came from, and losing the render is not losing the page.
            page.image = None

    if not any(p.image for p in selected) and not any(p.has_text for p in selected):
        raise PdfError(
            "Nothing could be read from that PDF - no text and no page would "
            "render. If it is password protected, remove the password first."
        )

    chosen = {p.number for p in selected}
    return PdfRead(
        info=meta, pages=selected, total_pages=meta.pages,
        skipped=[n for n in range(1, meta.pages + 1) if n not in chosen],
    )
