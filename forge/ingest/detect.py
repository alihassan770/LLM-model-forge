"""What kind of file is this, really?

Four formats are accepted - PNG, JPEG, WebP, PDF - because those are exactly
what the model providers ingest natively. Nothing sits between the user's file
and the model, so nothing is lost or distorted on the way in.

Type is decided from the **bytes**, never from the filename or the browser's
Content-Type. Both are supplied by the client and both are routinely wrong: a
phone renames a JPEG to .png, an export tool mislabels a PDF, and a hostile
uploader lies on purpose.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PNG = "image/png"
JPEG = "image/jpeg"
WEBP = "image/webp"
PDF = "application/pdf"

ACCEPTED = (PNG, JPEG, WEBP, PDF)

LABELS = {PNG: "PNG", JPEG: "JPEG", WEBP: "WebP", PDF: "PDF"}
EXTENSIONS = {PNG: "png", JPEG: "jpg", WEBP: "webp", PDF: "pdf"}

# Formats people will genuinely try, so the refusal can name the fix rather
# than saying "unsupported file type" and leaving them stuck.
KNOWN_REJECTS = {
    b"\x00\x00\x00\x18ftypheic": ("HEIC", "convert it to JPEG or PNG"),
    b"\x00\x00\x00\x18ftypheix": ("HEIC", "convert it to JPEG or PNG"),
    b"\x00\x00\x00\x1cftypheic": ("HEIC", "convert it to JPEG or PNG"),
    b"\x00\x00\x00\x18ftypmif1": ("HEIF", "convert it to JPEG or PNG"),
    b"II*\x00": ("TIFF", "convert it to PNG"),
    b"MM\x00*": ("TIFF", "convert it to PNG"),
    b"BM": ("BMP", "convert it to PNG"),
    b"GIF87a": ("GIF", "convert it to PNG"),
    b"GIF89a": ("GIF", "convert it to PNG"),
    b"PK\x03\x04": ("a Zip or Office file", "upload the photo or PDF itself"),
    b"AC10": ("AutoCAD DWG", "export it as a PDF"),
}

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024


class UnsupportedFile(Exception):
    """Carries a message the uploader can act on."""


@dataclass
class Detected:
    mime: str
    width: int | None = None
    height: int | None = None
    page_count: int | None = None

    @property
    def is_pdf(self) -> bool:
        return self.mime == PDF

    @property
    def label(self) -> str:
        return LABELS[self.mime]

    @property
    def extension(self) -> str:
        return EXTENSIONS[self.mime]


def sniff(head: bytes) -> str | None:
    """Return the MIME type from the leading bytes, or None if unrecognised."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head.startswith(b"%PDF-"):
        return PDF
    return None


def _reject(head: bytes) -> None:
    for signature, (name, advice) in KNOWN_REJECTS.items():
        if head.startswith(signature):
            raise UnsupportedFile(
                f"That looks like {name}, which we cannot read. "
                f"Please {advice} and upload it again."
            )
    raise UnsupportedFile(
        "We accept PNG, JPEG, WebP and PDF files. That file is none of those."
    )


# --------------------------------------------------------------------------- #
# Dimensions, read from the header only
# --------------------------------------------------------------------------- #

def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segment chain to the first frame header."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # Standalone markers carry no length.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        # SOF0..SOF15, excluding the DHT/JPG/DAC markers interleaved in range.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if length <= 0:
            return None
        i += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return w, h
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return None


def _pdf_pages(data: bytes) -> int | None:
    """A cheap page count without a PDF library.

    Counts '/Type /Page' occurrences, which is right for ordinary documents and
    approximate for ones using object streams. It is only used to show the user
    a number and to enforce a cap, so approximate is acceptable here - the real
    parse happens in the worker.
    """
    count = data.count(b"/Type/Page") + data.count(b"/Type /Page")
    kids = data.count(b"/Type/Pages") + data.count(b"/Type /Pages")
    pages = count - kids
    return pages if pages > 0 else None


def inspect(data: bytes, declared_mime: str = "") -> Detected:
    """Identify the file and read what can be read from its header.

    Raises UnsupportedFile with a message intended for the person uploading.
    """
    if not data:
        raise UnsupportedFile("That file is empty.")

    mime = sniff(data[:32])
    if mime is None:
        _reject(data[:32])

    limit = MAX_PDF_BYTES if mime == PDF else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise UnsupportedFile(
            f"That file is {len(data) / 1024 / 1024:.1f} MB. "
            f"The limit for {LABELS[mime]} is {limit // 1024 // 1024} MB."
        )

    if mime == PDF:
        return Detected(mime=PDF, page_count=_pdf_pages(data))

    size = {PNG: _png_size, JPEG: _jpeg_size, WEBP: _webp_size}[mime](data)
    if size is None:
        raise UnsupportedFile(
            f"That {LABELS[mime]} file appears to be damaged - we could not "
            f"read its dimensions."
        )

    width, height = size
    if width < 64 or height < 64:
        raise UnsupportedFile(
            f"That image is only {width}×{height} pixels. "
            f"Dimensions cannot be read reliably from an image that small."
        )

    # A decompression-bomb guard: a small file can declare an enormous canvas,
    # and anything that decodes it later would allocate width × height × 4 bytes.
    if width * height > 80_000_000:
        raise UnsupportedFile(
            f"That image declares {width}×{height} pixels, which is too large "
            f"to process safely."
        )

    return Detected(mime=mime, width=width, height=height)
