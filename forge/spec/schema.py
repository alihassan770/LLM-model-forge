"""The PartSpec - the single source of truth for a modelled object.

Everything downstream reads this document: the validator, the geometry builder,
the bill of materials. It is produced by a vision model under a JSON Schema, so
the response is schema-valid or the request fails loudly.

Two structural decisions worth knowing:

* **Parts are a flat list with `parent_id`, not nested.** Structured outputs do
  not support recursive schemas, and a flat list is easier to validate, diff and
  edit anyway. The underside pad of a leg is its own part whose parent is the
  leg - the hierarchy is data, not nesting.

* **Dimensions are an array, not an object.** A schema with
  `additionalProperties: false` cannot accept arbitrary keys like `top_diameter`,
  so each dimension carries its own `name`. That also gives every measurement
  somewhere to record where it came from.
"""

from __future__ import annotations

# Where a number came from, ordered by how much it can be trusted.
SOURCES = ["user", "pdf_text_layer", "image_label", "derived", "estimated"]
EXACT_SOURCES = {"user", "pdf_text_layer"}

PRIMITIVES = ["box", "cylinder", "tube", "revolve", "extrude", "sphere",
              "cushion", "sweep"]

# The planes a swept part's centre line can be drawn in. Almost every curve in
# furniture is flat - an arm loop, a sled base, a cabriole leg, a bentwood back
# are all drawn in one plane and then given a section - so a flat path plus a
# named plane covers the real cases without asking a model to reason in three
# dimensions at once, which it cannot do reliably.
SWEEP_PLANES = ["xz", "yz", "xy"]
ROLES = ["structural", "surface", "upholstery", "hardware", "decorative"]

UNITS = ["mm", "cm", "m", "in", "ft"]

# Millimetres are canonical everywhere inside the system: integer-friendly,
# unambiguous, and the furniture industry standard. Display units are a
# per-organisation setting, applied at the edge.
TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


def to_mm(value: float, unit: str) -> float:
    return float(value) * TO_MM.get(unit, 1.0)


_MEASUREMENT = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "length, width, height, diameter, thickness, radius…",
        },
        "value": {"type": "number"},
        "unit": {"type": "string", "enum": UNITS},
        "source": {"type": "string", "enum": SOURCES},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["name", "value", "unit", "source", "confidence", "note"],
    "additionalProperties": False,
}

# --------------------------------------------------------------------------- #
# Where a part sits, how it is finished, and what shape it really is
# --------------------------------------------------------------------------- #
#
# These three blocks are the difference between a pile of correctly-sized
# offcuts and something that looks like the photograph. Before they existed the
# specification said what every piece was and how big it was, and said nothing
# about where it went - so the assembly was reconstructed downstream by
# matching words in the part's id ("apron" in the name means it goes under the
# top). That guessing works for the furniture whose vocabulary was anticipated
# and produces nonsense for the rest, while the model that could simply have
# been asked had already looked at the object.

# The object's own frame, used by every anchor below. Fixed and documented
# because a coordinate is meaningless without one:
#
#   X  left (0) to right (1), the widest horizontal direction
#   Y  front (0) to back (1)
#   Z  floor (0) to the object's full height (1)
#
# Fractions rather than millimetres on purpose. A model asked for "the leg's
# centre is 47mm from the left edge" has to do arithmetic it cannot check; a
# model asked "is that leg at the left edge or the middle?" is reading the
# picture, which is the thing it is good at. The fractions are multiplied by the
# measured bounding box afterwards, so the numbers stay exact.
ALIGNMENTS = ["min", "center", "max"]
RUN_AXES = ["x", "y", "z"]

# How the copies of a part with count > 1 are distributed. Naming the
# arrangement is far more reliable than asking for one anchor per copy, and it
# keeps a set of four legs symmetrical by construction rather than by luck.
PATTERNS = [
    "single",      # count is 1, or every copy sits in the same place
    "mirror_x",    # a pair, left and right
    "mirror_y",    # a pair, front and back
    "mirror_xy",   # four, one per corner - the usual case for legs
    "row_x",       # evenly spaced along X, e.g. shelf pins or spindles
    "row_y",       # evenly spaced along Y, e.g. drawer runners
    "stack_z",     # evenly spaced up Z, e.g. shelves or a bank of drawers
    "radial",      # spun about the centre, e.g. a five-star chair base
    "radial_x",    # spun about the X axis, e.g. the spokes of a bicycle wheel
    "radial_y",    # spun about the Y axis
]

# Which way the axis of a radial pattern points. Plain `radial` spins about Z,
# because that is what a chair base, a pedestal and a round table do.
RADIAL_AXES = {"radial": "z", "radial_x": "x", "radial_y": "y"}

_PLACEMENT = {
    "type": "object",
    "properties": {
        "anchor_x": {
            "type": "number",
            "description": "Across the object: 0 is the left edge, 0.5 the "
                           "centre, 1 the right edge.",
        },
        "anchor_y": {
            "type": "number",
            "description": "Front to back: 0 is the front face, 1 the back.",
        },
        "anchor_z": {
            "type": "number",
            "description": "Floor to full height: 0 is the floor, 1 the top.",
        },
        "align_x": {
            "type": "string",
            "enum": ALIGNMENTS,
            "description": "Which part of this piece sits on anchor_x. "
                           "'center' for nearly everything; 'min' or 'max' "
                           "when a face is flush with the anchor.",
        },
        "align_y": {"type": "string", "enum": ALIGNMENTS},
        "align_z": {
            "type": "string",
            "enum": ALIGNMENTS,
            "description": "'min' for anything standing ON the floor or a "
                           "shelf, 'max' for a top whose upper face is the "
                           "object's height, 'center' otherwise. Getting this "
                           "wrong sinks a part half its thickness into what it "
                           "rests on.",
        },
        "run_axis": {
            "type": "string",
            "enum": RUN_AXES,
            "description": "Which way the part's LONGEST dimension points in "
                           "the assembled object. A table leg runs 'z'; a rail "
                           "along the front runs 'x'; a drawer runner pointing "
                           "backwards runs 'y'. This is what stops a 160mm bar "
                           "handle being built as a 160mm vertical post.",
        },
        "tilt_x": {
            "type": "number",
            "description": "Degrees of lean about the X axis. A chair back "
                           "raked 15 degrees backwards is -15.0 for anything "
                           "upright or flat, which is most parts.",
        },
        "tilt_y": {
            "type": "number",
            "description": "Degrees of lean about the Y axis, e.g. legs "
                           "splayed outwards to the left and right.",
        },
        "tilt_z": {
            "type": "number",
            "description": "Degrees of rotation in plan view. 0 unless the "
                           "part is turned on the floor plan.",
        },
        "pattern": {
            "type": "string",
            "enum": PATTERNS,
            "description": "How this part's copies are arranged. Four legs at "
                           "the corners is 'mirror_xy' with the anchor giving "
                           "ONE corner - the other three are reflected from it.",
        },
        "spacing_mm": {
            "type": "number",
            "description": "Centre-to-centre gap for row_x, row_y and stack_z. "
                           "0 for the other patterns, or to spread the copies "
                           "evenly across the object instead.",
        },
        "confidence": {
            "type": "number",
            "description": "How sure you are of this position, 0-1. Below 0.4 "
                           "the part is laid out beside the object rather than "
                           "placed wrongly inside it.",
        },
    },
    "required": ["anchor_x", "anchor_y", "anchor_z", "align_x", "align_y",
                 "align_z", "run_axis", "tilt_x", "tilt_y", "tilt_z",
                 "pattern", "spacing_mm", "confidence"],
    "additionalProperties": False,
}

FINISHES = ["matte", "satin", "gloss", "metallic", "transparent",
            "textile", "raw"]

# Which face of a part printed artwork sits on, in the object's own frame.
FACES = ["front", "back", "left", "right", "top", "bottom"]

# The two extents that span each face, as indices into a part's (x, y, z), in
# the order (across, up) as the artwork is read. Kept here rather than beside
# the Blender code because the runner needs it too - it pads each crop to the
# shape of the face it is going onto, and both have to agree about which face
# is which.
FACE_AXES = {
    "front": (0, 2), "back": (0, 2),
    "left": (1, 2), "right": (1, 2),
    "top": (0, 1), "bottom": (0, 1),
}

_ARTWORK = {
    "type": "object",
    "properties": {
        "image_index": {
            "type": "integer",
            "description": "Which supplied image the artwork is cut from, "
                           "using the 'Image N:' numbers in the request.",
        },
        "x": {"type": "number",
              "description": "Left edge of the artwork in that image, 0-1."},
        "y": {"type": "number",
              "description": "Top edge, 0-1 down the image."},
        "w": {"type": "number", "description": "Width as a fraction, 0-1."},
        "h": {"type": "number", "description": "Height as a fraction, 0-1."},
        "face": {
            "type": "string",
            "enum": FACES,
            "description": "Which face of the part the artwork is printed on. "
                           "'front' for a poster, a shelf talker or a logo on "
                           "the front of a header.",
        },
        "found": {
            "type": "boolean",
            "description": "False when this part carries no printed artwork - "
                           "a plain shelf, a side panel, a rail. The other "
                           "fields are then ignored.",
        },
        "describes": {
            "type": "string",
            "description": "What the artwork is, in a few words: 'Dove "
                           "wordmark and bird logo', 'model photograph', "
                           "'HAIR REBORN 10x STRONGER lockup'.",
        },
    },
    "required": ["image_index", "x", "y", "w", "h", "face", "found",
                 "describes"],
    "additionalProperties": False,
}

_APPEARANCE = {
    "type": "object",
    "properties": {
        "hex": {
            "type": "string",
            "description": "The part's colour as seen, '#RRGGBB'. Read it off "
                           "the photograph rather than from what the material "
                           "is usually like: a painted oak leg is the colour "
                           "of the paint. Judge it in even light and ignore "
                           "highlights and shadow.",
        },
        "finish": {
            "type": "string",
            "enum": FINISHES,
            "description": "How the surface handles light: 'metallic' for bare "
                           "or plated metal, 'gloss' for lacquer and glass, "
                           "'textile' for fabric, 'matte' for most painted and "
                           "oiled surfaces.",
        },
        "source": {
            "type": "string",
            "enum": ["observed", "assumed"],
            "description": "'observed' when you can see this part's colour, "
                           "'assumed' when it is hidden and you inferred it "
                           "from a matching part.",
        },
        "note": {
            "type": "string",
            "description": "Grain, weave, pattern or anything a flat colour "
                           "cannot carry.",
        },
    },
    "required": ["hex", "finish", "source", "note"],
    "additionalProperties": False,
}

_SHAPE = {
    "type": "object",
    "properties": {
        "taper": {
            "type": "number",
            "description": "How much narrower the FAR end is than the near "
                           "end, as a fraction: 1.0 is parallel-sided, 0.6 is "
                           "a leg that narrows to 60% at the foot, 1.3 flares "
                           "outwards. Tapered legs are extremely common and a "
                           "parallel box reads as flat-pack where the real "
                           "thing reads as furniture.",
        },
        "edge_radius_mm": {
            "type": "number",
            "description": "Radius of the rounding on the part's edges. 0 for "
                           "a sharp sawn edge, 2-3 for a normal eased edge, "
                           "and up to half the thickness for a fully rounded "
                           "one. Cheap to apply and it changes how the whole "
                           "object reads.",
        },
        "profile": {
            "type": "array",
            "description": "For a turned or moulded part only: the silhouette, "
                           "as a handful of points from one end to the other. "
                           "Leave empty for anything with a constant section.",
            "items": {
                "type": "object",
                "properties": {
                    "at": {
                        "type": "number",
                        "description": "How far along the part, 0 at the near "
                                       "end and 1 at the far end.",
                    },
                    "scale": {
                        "type": "number",
                        "description": "The width there, as a fraction of the "
                                       "part's stated diameter or width.",
                    },
                },
                "required": ["at", "scale"],
                "additionalProperties": False,
            },
        },
        "path": {
            "type": "array",
            "description": "For a `sweep` part only: the centre line the "
                           "section follows, as a handful of points tracing "
                           "the curve. This is what makes a bent arm a loop "
                           "rather than a post. Leave empty for every other "
                           "primitive.",
            "items": {
                "type": "object",
                "properties": {
                    "u": {
                        "type": "number",
                        "description": "Across the part's bounding box in the "
                                       "first axis of `path_plane`, 0 to 1.",
                    },
                    "v": {
                        "type": "number",
                        "description": "Across the second axis of "
                                       "`path_plane`, 0 to 1.",
                    },
                },
                "required": ["u", "v"],
                "additionalProperties": False,
            },
        },
        "path_plane": {
            "type": "string",
            "enum": SWEEP_PLANES,
            "description": "Which flat plane the curve is drawn in, seen from "
                           "the obvious view: 'xz' is the front elevation, "
                           "'yz' the side elevation (an armrest loop or a "
                           "sled base leg), 'xy' the plan.",
        },
        "path_closed": {
            "type": "boolean",
            "description": "True when the curve returns to where it started - "
                           "a closed armrest hoop, a ring. False for an open "
                           "curve like a cabriole leg or a bent back post.",
        },
    },
    "required": ["taper", "edge_radius_mm", "profile", "path", "path_plane",
                 "path_closed"],
    "additionalProperties": False,
}

_PART = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Stable dotted identifier, e.g. leg.front_left "
                           "or frame.rail.front. Lower case, no spaces.",
        },
        "display_name": {"type": "string"},
        "parent_id": {
            "type": "string",
            "description": "id of the part this belongs to, or empty string.",
        },
        "role": {"type": "string", "enum": ROLES},
        "primitive": {"type": "string", "enum": PRIMITIVES},
        "material": {
            "type": "string",
            "description": "Short code, e.g. hardwood_oak, steel_tube, "
                           "fabric_linen, nylon_glide.",
        },
        "count": {"type": "integer"},
        "dimensions": {"type": "array", "items": _MEASUREMENT},
        "placement": _PLACEMENT,
        "appearance": _APPEARANCE,
        "shape": _SHAPE,
        "artwork": _ARTWORK,
        "notes": {"type": "string"},
    },
    "required": ["id", "display_name", "parent_id", "role", "primitive",
                 "material", "count", "dimensions", "placement", "appearance",
                 "shape", "artwork", "notes"],
    "additionalProperties": False,
}

# Normalised 0-1 coordinates so the crop survives any later resize of the
# source image. Pixel coordinates would silently point at the wrong place the
# moment the file is scaled.
_REGION = {
    "type": "object",
    "properties": {
        "image_index": {
            "type": "integer",
            "description": "Which supplied image this region is in, using the "
                           "'Image N:' numbers given in the request. A PDF "
                           "contributes one image per page, so this is not "
                           "always the same as the file it came from.",
        },
        "x": {"type": "number", "description": "Left edge, 0-1 across the image."},
        "y": {"type": "number", "description": "Top edge, 0-1 down the image."},
        "w": {"type": "number", "description": "Width as a fraction, 0-1."},
        "h": {"type": "number", "description": "Height as a fraction, 0-1."},
        "found": {
            "type": "boolean",
            "description": "False when the part is not visible in any image; "
                           "the others are then ignored.",
        },
    },
    "required": ["image_index", "x", "y", "w", "h", "found"],
    "additionalProperties": False,
}

_MISSING = {
    "type": "object",
    "properties": {
        "part_id": {"type": "string"},
        "region": _REGION,
        "dimension": {"type": "string"},
        "question": {
            "type": "string",
            "description": "Asked of a non-expert, in plain language, naming "
                           "the part and where to measure it.",
        },
        "why_needed": {
            "type": "string",
            "description": "Why the model cannot be built without it. People "
                           "answer far more readily when they know the reason.",
        },
        "unit": {"type": "string", "enum": UNITS},
        "suggested_min": {"type": "number"},
        "suggested_max": {"type": "number"},
        "blocking": {
            "type": "boolean",
            "description": "True when geometry cannot be generated without it.",
        },
    },
    "required": ["part_id", "region", "dimension", "question", "why_needed",
                 "unit", "suggested_min", "suggested_max", "blocking"],
    "additionalProperties": False,
}

# How parts are joined. This is what makes a fastener count a *calculation*
# rather than a guess: "20 screws, of which 10 are 50mm" falls out of eight
# corner joints in 18mm material, and changes by itself when a dimension does.
JOINT_TYPES = [
    "butt_screwed", "corner_block", "pocket_screw", "dowelled",
    "mortise_tenon", "bolted", "nailed", "stapled", "welded",
    "glued", "bracket", "press_fit",
]

_JOINT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "type": {"type": "string", "enum": JOINT_TYPES},
        "parts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The part ids being joined, two or more.",
        },
        "count": {
            "type": "integer",
            "description": "How many times this joint occurs. Four identical "
                           "leg-to-apron corners is one entry with count 4.",
        },
        "notes": {"type": "string"},
    },
    "required": ["id", "type", "parts", "count", "notes"],
    "additionalProperties": False,
}

PARTSPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "object": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "sofa, table, chair, cabinet, desk…",
                },
                "subtype": {"type": "string"},
                "style_notes": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": "One sentence a human would recognise the "
                                   "object from.",
                },
            },
            "required": ["category", "subtype", "style_notes", "summary"],
            "additionalProperties": False,
        },
        "overall": {"type": "array", "items": _MEASUREMENT},
        "parts": {"type": "array", "items": _PART},
        "joints": {"type": "array", "items": _JOINT},
        "missing_dimensions": {"type": "array", "items": _MISSING},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "readability": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "Anything that limited the reading: blur, "
                                   "occlusion, labels too small to resolve.",
                },
                "labels_found": {
                    "type": "boolean",
                    "description": "True if printed dimension labels were "
                                   "visible in the file.",
                },
            },
            "required": ["notes", "labels_found"],
            "additionalProperties": False,
        },
    },
    "required": ["object", "overall", "parts", "joints", "missing_dimensions",
                 "assumptions", "readability"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Reading helpers
# --------------------------------------------------------------------------- #

def measurement_mm(m: dict) -> float | None:
    try:
        return to_mm(m["value"], m.get("unit", "mm"))
    except (TypeError, ValueError, KeyError):
        return None


def is_exact(m: dict) -> bool:
    return m.get("source") in EXACT_SOURCES


def blocking_questions(spec: dict) -> list[dict]:
    return [q for q in spec.get("missing_dimensions", []) if q.get("blocking")]


def part_count(spec: dict) -> int:
    return sum(max(1, p.get("count", 1)) for p in spec.get("parts", []))


def confidence_band(value: float) -> str:
    if value >= 0.85:
        return "high"
    if value >= 0.55:
        return "medium"
    return "low"


# --------------------------------------------------------------------------- #
# Placement, appearance and shape: reading them safely
# --------------------------------------------------------------------------- #
#
# Every spec written before these blocks existed is still in the database and
# still has to build. So nothing downstream may index into them directly -
# it goes through the three readers here, which return a complete document
# whatever they are given. A missing block is not an error; it means "this was
# read before we knew to ask", and the geometry falls back to the conventions
# it used then.

# Below this, a position is treated as a guess rather than an observation and
# the part is laid out beside the object instead of placed inside it. A part in
# the wrong place looks like a modelling error and gets trusted; a part sitting
# next to the object is visibly unplaced and gets moved.
PLACEMENT_CONFIDENCE_FLOOR = 0.4


def _number(value, default: float, low: float | None = None,
            high: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN or infinity
        return default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def placement_of(part: dict) -> dict | None:
    """A part's placement, normalised - or None if it never had one.

    None and "a placement I do not trust" are deliberately different answers.
    None means the spec predates the field and the caller should use its own
    conventions; a low confidence means the model looked and was unsure, which
    is a fact worth keeping rather than silently overwriting.
    """
    raw = part.get("placement")
    if not isinstance(raw, dict):
        return None

    def align(key: str) -> str:
        value = str(raw.get(key, "center")).lower()
        return value if value in ALIGNMENTS else "center"

    axis = str(raw.get("run_axis", "")).lower()
    pattern = str(raw.get("pattern", "")).lower()

    return {
        # Anchors are clamped a little outside 0-1 rather than exactly to it:
        # a castor genuinely sits below the floor line of the body it carries,
        # and a handle genuinely projects past the front face.
        "anchor_x": _number(raw.get("anchor_x"), 0.5, -0.5, 1.5),
        "anchor_y": _number(raw.get("anchor_y"), 0.5, -0.5, 1.5),
        "anchor_z": _number(raw.get("anchor_z"), 0.5, -0.5, 1.5),
        "align_x": align("align_x"),
        "align_y": align("align_y"),
        "align_z": align("align_z"),
        "run_axis": axis if axis in RUN_AXES else "",
        # A part rotated more than a right angle has almost always been
        # described in a frame other than the one asked for, and honouring it
        # lies the piece flat. Tilt is a lean, not a reorientation - that is
        # what run_axis is for.
        "tilt_x": _number(raw.get("tilt_x"), 0.0, -90.0, 90.0),
        "tilt_y": _number(raw.get("tilt_y"), 0.0, -90.0, 90.0),
        "tilt_z": _number(raw.get("tilt_z"), 0.0, -180.0, 180.0),
        "pattern": pattern if pattern in PATTERNS else "single",
        # Carried alongside the pattern so `anchors` need not know the naming.
        "spin_axis": RADIAL_AXES.get(pattern, "z"),
        "spacing_mm": _number(raw.get("spacing_mm"), 0.0, 0.0, 10000.0),
        "confidence": _number(raw.get("confidence"), 0.5, 0.0, 1.0),
    }


def placed_confidently(part: dict) -> bool:
    placement = placement_of(part)
    return (placement is not None
            and placement["confidence"] >= PLACEMENT_CONFIDENCE_FLOOR)


_HEX_DIGITS = set("0123456789abcdef")


def parse_hex(value) -> tuple[float, float, float] | None:
    """'#RRGGBB' to linear-ish 0-1 floats, or None if it is not a colour.

    The three-digit form is accepted because it is written often enough to be
    worth handling, and anything else is rejected outright: a colour that is
    nearly a colour is how a part ends up black.
    """
    text = str(value or "").strip().lstrip("#").lower()
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6 or any(c not in _HEX_DIGITS for c in text):
        return None
    return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def appearance_of(part: dict) -> dict:
    """A part's colour and finish. Always a document; `hex` may be None."""
    raw = part.get("appearance")
    raw = raw if isinstance(raw, dict) else {}
    finish = str(raw.get("finish", "")).lower()
    source = str(raw.get("source", "")).lower()
    return {
        "rgb": parse_hex(raw.get("hex")),
        "hex": (str(raw.get("hex") or "").strip() or None),
        "finish": finish if finish in FINISHES else "",
        "source": source if source in ("observed", "assumed") else "assumed",
        "note": str(raw.get("note", ""))[:400],
    }


def artwork_of(part: dict) -> dict | None:
    """The printed artwork on a part, or None if it carries none.

    A retail display is mostly print: a Dove header is a white box until the
    wordmark is on it, and a backlit poster is a blank panel until the
    photograph is. The model reads all of that correctly - it named the
    wordmark, the bird, the "HAIR REBORN 10x STRONGER" lockup and the model
    photograph - and every one of them became a flat-coloured box, because
    there was nowhere to put an image.

    The region is described exactly like a question's crop region, against the
    same `Image N:` numbering, so the same machinery that cuts a crop for a
    question cuts the artwork for a face.
    """
    raw = part.get("artwork")
    if not isinstance(raw, dict) or not raw.get("found"):
        return None

    face = str(raw.get("face", "front")).lower()
    try:
        index = max(0, int(raw.get("image_index", 0)))
    except (TypeError, ValueError):
        index = 0

    width = _number(raw.get("w"), 0.0, 0.0, 1.0)
    height = _number(raw.get("h"), 0.0, 0.0, 1.0)
    # A region with no area cannot be cut, and a one-pixel sliver stretched
    # over a poster is worse than leaving the panel its flat colour.
    if width < 0.01 or height < 0.01:
        return None

    return {
        "image_index": index,
        "x": _number(raw.get("x"), 0.0, 0.0, 1.0),
        "y": _number(raw.get("y"), 0.0, 0.0, 1.0),
        "w": width,
        "h": height,
        "face": face if face in FACES else "front",
        "describes": str(raw.get("describes", ""))[:200],
    }


def shape_of(part: dict) -> dict:
    """A part's taper, edge rounding and turned profile, with sane bounds."""
    raw = part.get("shape")
    raw = raw if isinstance(raw, dict) else {}

    profile = []
    for point in raw.get("profile") if isinstance(raw.get("profile"), list) else []:
        if not isinstance(point, dict):
            continue
        profile.append({
            "at": _number(point.get("at"), 0.0, 0.0, 1.0),
            # A profile point of zero closes the solid to nothing and Blender
            # produces a degenerate face there, so the floor is a hair above it.
            "scale": _number(point.get("scale"), 1.0, 0.02, 4.0),
        })
    profile.sort(key=lambda p: p["at"])

    # The swept centre line. Points are kept in the order given - unlike a
    # turned profile, which is sorted, because the order of a path IS the
    # curve. Sorting an arm loop by one coordinate would untie it.
    path = []
    for point in raw.get("path") if isinstance(raw.get("path"), list) else []:
        if not isinstance(point, dict):
            continue
        path.append({
            "u": _number(point.get("u"), 0.0, -0.2, 1.2),
            "v": _number(point.get("v"), 0.0, -0.2, 1.2),
        })

    plane = str(raw.get("path_plane", "")).lower()

    return {
        # A taper below 0.05 is a point rather than a foot, and above 3 the
        # part stops resembling what was measured.
        "taper": _number(raw.get("taper"), 1.0, 0.05, 3.0),
        "edge_radius_mm": _number(raw.get("edge_radius_mm"), 0.0, 0.0, 200.0),
        "profile": profile if len(profile) >= 2 else [],
        # Two points are a straight line, which the box and cylinder
        # primitives already do better; a sweep needs at least three to be
        # worth the curve machinery.
        "path": path if len(path) >= 3 else [],
        "path_plane": plane if plane in SWEEP_PLANES else "xz",
        "path_closed": bool(raw.get("path_closed", False)),
    }
