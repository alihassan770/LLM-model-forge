"""Where each part sits in space.

The specification describes parts and their sizes; it does not reliably say
where they go. Rather than guess a full assembly - which would produce
confidently wrong geometry - this module places parts by the role and name
conventions that hold for most furniture, and otherwise stacks them clearly
apart so a modeller can see every piece and move it.

That choice is deliberate. A half-correct assembly looks finished and hides its
errors; an obviously laid-out set of correctly-sized parts looks like what it
is - a starting point with every dimension right.
"""

from __future__ import annotations

# build.py puts the repository root on sys.path before importing this, so a
# package-qualified import works inside Blender as well as outside it.
from forge.build import anchors
from forge.build import carcass
from forge.build import seating
from forge.spec import dims as spec_dims
from forge.spec import schema as spec_schema

# How much of the object the model must have placed confidently before its own
# assembly is used instead of the conventions in this file.
#
# It is a majority rather than a simple "use it if present" because a spec with
# three placed parts and twenty guessed ones assembles into something worse
# than either approach applied consistently: the placed parts move to where
# they were seen, the rest stay where the rules put them, and the two frames of
# reference do not agree. Below the threshold the model's positions are still
# used - but only for parts these rules have no convention for, where the
# alternative is the row of unplaced pieces beside the object.
TRUSTED_PLACEMENT_SHARE = 0.7

TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


def _mm(measurement: dict) -> float:
    try:
        return float(measurement["value"]) * TO_MM.get(measurement.get("unit", "mm"), 1.0)
    except (TypeError, ValueError, KeyError):
        return 0.0


def _axes(part: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in part.get("dimensions", []):
        name = str(d.get("name", "")).lower()
        value = _mm(d)
        if value <= 0:
            continue
        for axis, words in (
            ("length", ("length", "len", "span")),
            ("width", ("width", "wide")),
            ("depth", ("depth", "deep")),
            ("height", ("height", "tall", "rise")),
            ("thickness", ("thick",)),
            ("diameter", ("diameter", "dia")),
        ):
            if any(w in name for w in words) and axis not in out:
                out[axis] = value
                break
    return out


# Overall measurements that are not a horizontal extent of the object. Without
# this filter a 30mm top thickness or a 90mm overhang can be mistaken for one
# of the two footprint sides.
_NOT_A_SPAN = ("height", "thick", "overhang", "clearance", "gap", "inset",
               "leg", "seat", "diameter", "radius")

# Words that make a measurement belong to a PART rather than to the object.
#
# "carcass width: 1760" is the inside of the box; "door width: 875" is a door.
# Neither is a side of the object, and both are horizontal, so a rule that
# picks the two largest horizontal measurements picks 1800 and 1760 - two
# measurements of the SAME direction - and never sees the 450mm depth at all.
#
# That is what happened to the Oakley sideboard: y_span came out as 1760
# instead of 450, so every part placed by a fraction of the depth was thrown
# about a metre out and the model exploded. The parts were all the right size,
# which is what made it look like a placement bug rather than a measurement
# one.
_PART_QUALIFIED = (
    "carcass", "door", "drawer", "shelf", "handle", "plinth", "panel",
    "apron", "rail", "arm", "cushion", "runner", "hinge", "glide", "foot",
    "back", "top", "base", "frame", "internal", "inside", "opening", "clear",
)

_DEPTH_WORDS = ("depth", "deep")


def _round_footprint(named: dict[str, float]) -> tuple[float, float] | None:
    """The plan of a round object, from the widest diameter it states.

    `_NOT_A_SPAN` excludes anything called a diameter, which is right for a
    table - a leg's diameter is not the table's footprint - and leaves a stool
    with no horizontal measurement at all. It fell back to a 1200 x 700
    default, so a 340mm stool was assembled inside a footprint the size of a
    dining table and its legs were flung most of a metre apart.

    For an object whose plan really is a circle, the widest diameter anywhere
    in its overall measurements is that plan: a seat 340 across on a foot ring
    300 across is 340 wide and 340 deep.
    """
    diameters = [v for n, v in named.items()
                 if ("diameter" in n or "dia." in n) and v > 0]
    if not diameters:
        return None
    widest = max(diameters)
    return widest, widest


def _footprint(spans: list[tuple[str, float]]) -> tuple[float, float]:
    """The object's two horizontal sides, as (x, y) in millimetres.

    Size alone is not enough and name alone is not enough, so both are used.

    Name alone fails because makers disagree: a table is "length 1520, width
    910" and a cabinet is "width 1800, depth 450". Both mean the long way and
    the short way, and looking for `width` and `depth` reads the table's long
    side as its short one.

    Size alone fails when the spec also carries a part's horizontal size, which
    a good extraction always does - and an internal width is very close to the
    external one, so it outranks the genuine second side.

    So: prefer measurements that name no part, and within those let an
    explicit depth claim the short axis. Everything falls back to the old
    largest-two rule when there is nothing better to go on.
    """
    if not spans:
        return 1200.0, 700.0

    unqualified = [(n, v) for n, v in spans
                   if not any(word in n for word in _PART_QUALIFIED)]
    # Only trusted when it leaves enough to choose from; one surviving
    # measurement says more about the naming than about the object.
    pool = unqualified if len(unqualified) >= 2 else spans

    depths = [v for n, v in pool if any(w in n for w in _DEPTH_WORDS)]
    others = [v for n, v in pool if not any(w in n for w in _DEPTH_WORDS)]
    if depths and others:
        # An explicit depth is the short side by definition, even on the rare
        # object that is deeper than it is wide.
        return max(others), max(depths)

    values = sorted((v for _, v in pool), reverse=True)
    if len(values) >= 2:
        return values[0], values[1]
    return values[0], values[0] * 0.6


def _named(spec: dict) -> dict[str, float]:
    """The spec's overall measurements by name, in millimetres.

    `_overall` below reduces these to a footprint and a height, which is all a
    table needs. A chair needs the names themselves - "seat_height_min",
    "base_diameter", "armrest_height_above_seat" are the measurements the
    drawing actually labels, and they place the parts far better than any
    proportion of the total height could.
    """
    named: dict[str, float] = {}
    for m in spec.get("overall", []):
        value = _mm(m)
        if value > 0:
            named.setdefault(str(m.get("name", "")).lower(), value)
    return named


def _overall(spec: dict) -> dict[str, float]:
    """The object's footprint, height and leg frame, all in millimetres.

    The hard part is that a spec does not name its two horizontal sides
    consistently. A table labelled by its maker says *length* 1520 and *width*
    910; a cabinet says *width* 1800 and *depth* 450. Both mean "the long way"
    and "the short way".

    This used to look for `width` and `depth` by name, so the table's 1520
    length was never read at all: X became 910 - the short side - and Y fell
    back to a 350mm default. Four legs then landed in a 728 x 280 footprint
    under a 1520 x 910 top, bunched under the middle, and the aprons sat so far
    inboard they were hidden beneath the top entirely.

    So the two sides are taken by **size, not by name**: the largest horizontal
    measurement is X and the next is Y. That is true of every naming convention
    at once, which is the only way to be right about all of them.
    """
    named: dict[str, float] = {}
    spans: list[tuple[str, float]] = []

    for m in spec.get("overall", []):
        name = str(m.get("name", "")).lower()
        value = _mm(m)
        if value <= 0:
            continue
        named.setdefault(name, value)
        if not any(word in name for word in _NOT_A_SPAN):
            spans.append((name, value))

    def first(*needles, default=0.0):
        """The value for the first needle that matches, exact names preferred.

        Substring matching alone picked `leg_height` (730) as the object's
        overall `height` (760), because "height" is inside "leg_height". A
        table 30mm too short is the kind of error that survives a review.
        """
        for needle in needles:
            if needle in named:
                return named[needle]
        for needle in needles:
            for name, value in named.items():
                if needle in name:
                    return value
        return default

    out: dict[str, float] = {}
    out["height"] = first("height", default=750.0)
    out["top_thickness"] = first("top_thickness", "top thickness", default=25.0)

    # The frame measurements are given by makers as the span between the legs - # "134 cm" along the long side of a 152 cm top. When present they are the
    # best possible input, because they say where the legs actually are instead
    # of leaving it to a percentage guess.
    frame = sorted((v for n, v in named.items()
                    if "frame" in n or "span" in n or "between" in n),
                   reverse=True)

    # Exclude anything already claimed as a frame span from the footprint, or a
    # frame length can outrank the top it sits under.
    usable = [(n, v) for n, v in spans if v not in frame]
    # A round object states no width or depth, only diameters - which the span
    # filter removes - so it would otherwise fall through to the default
    # footprint and be assembled at the wrong scale entirely.
    round_plan = _round_footprint(named) if not usable else None
    out["x_span"], out["y_span"] = round_plan or _footprint(usable)

    if len(frame) >= 2:
        out["frame_x"], out["frame_y"] = frame[0], frame[1]
    else:
        # No frame given: inset by the overhang if it was read, otherwise by a
        # conventional proportion.
        over_x = first("overhang_long", "overhang_end", default=0.0)
        over_y = first("overhang_side", "overhang_short", default=0.0)
        out["frame_x"] = (out["x_span"] - 2 * over_x) if over_x else out["x_span"] * 0.88
        out["frame_y"] = (out["y_span"] - 2 * over_y) if over_y else out["y_span"] * 0.88

    # Names the older rules below still read. Kept pointing at the footprint so
    # "width" means the long way and "depth" the short way, consistently.
    out["width"] = out["x_span"]
    out["depth"] = out["y_span"]
    return out


def place(spec: dict, trust_placement: bool | None = None,
          swap_plan: bool = False,
          ) -> dict[tuple[str, int], tuple[tuple, tuple]]:
    """Return {(part_id, instance_index): ((x, y, z), (rx, ry, rz))} in mm/degrees.

    Two kinds of object need two kinds of assembly. The rules below build a
    *frame*: legs at the corners of the footprint, rails between them, a top
    laid over the lot. That is a table, and for a table it is right.

    A cabinet is a box standing on something, and almost none of its parts can
    be placed from the footprint - its bottom sits at the top of its legs, its
    doors fill an opening whose size depends on the panels around it. Running
    a carcass through the frame rules gives correctly sized panels in
    meaningless positions, which is what the first sideboard build produced.
    So a carcass is assembled by `carcass.py` first, and whatever that does not
    claim - legs, glides, cushions - falls through to the frame rules here.
    """
    parts = spec.get("parts", [])
    overall = _overall(spec)

    # Some objects are long front-to-back rather than side-to-side.
    #
    # The footprint puts the largest horizontal measurement on X, which is
    # right for everything you stand beside - a table, a sideboard, a desk.
    # It is wrong for anything you sit in line with. A bicycle's long axis IS
    # its front-to-back direction, and the model says so correctly: it puts the
    # two wheels at `anchor_y` 0.2 and 0.8, `mirror_y`, because the front wheel
    # is at the front. With the 1713mm length on X and 440mm on Y that spread
    # came to 264mm and both wheels landed on top of each other.
    #
    # Which way round an object is cannot be settled from the measurements
    # alone, so it is not guessed here: `audit.best_placements` builds it both
    # ways and keeps whichever matches the stated dimensions better.
    if swap_plan:
        overall = dict(overall)
        overall["x_span"], overall["y_span"] = overall["y_span"], overall["x_span"]
        overall["frame_x"], overall["frame_y"] = overall["frame_y"], overall["frame_x"]
        overall["width"], overall["depth"] = overall["depth"], overall["width"]

    total_height = overall.get("height", 750.0)

    placements: dict[tuple[str, int], tuple[tuple, tuple]] = {}
    unplaced: list[dict] = []

    # Did the model tell us where the parts go?
    #
    # When it did, that beats everything below: the rules in this file infer an
    # assembly from the words in a part's id, and the model inferring it from
    # the photograph has seen the object. Rake, splay, a handle mounted
    # vertically, a shelf two thirds of the way up - none of those are
    # recoverable from a name, and all of them are visible in a picture.
    # `trust_placement` overrides the share rule: True uses every position the
    # model gave whatever its confidence, False ignores them entirely. Both
    # exist so the two assemblies can be built and *measured* against the
    # object's stated dimensions rather than chosen by a threshold somebody
    # picked - see `audit.best_placements`. None keeps the automatic rule.
    confident = [p for p in parts if spec_schema.placed_confidently(p)]
    described = [p for p in parts
                 if spec_schema.placement_of(p) is not None]

    if trust_placement is None:
        usable = confident
        trust_the_model = (
            bool(parts)
            and len(confident) >= max(1, int(len(parts) * TRUSTED_PLACEMENT_SHARE))
        )
    elif trust_placement:
        usable = described
        trust_the_model = bool(described)
    else:
        usable = []
        trust_the_model = False

    if trust_the_model:
        box = {"x_span": overall["x_span"], "y_span": overall["y_span"],
               "height": total_height}
        for part in usable:
            placement = spec_schema.placement_of(part)
            extent = spec_dims.box_extent(part)
            for index, pose in anchors.resolve(part, placement, extent, box).items():
                placements[(part["id"], index)] = pose

    # Which shape is this? A box, a chair, or a frame.
    #
    # Checked in that order and at most one applies. A carcass wins over
    # seating because a cabinet with a "seat.rail" is still a cabinet, while
    # nothing with a real carcass is a chair.
    #
    # Skipped entirely when the model placed the object itself: these two
    # assemble from a category's conventions, and running them over the top of
    # observed positions would overwrite what was seen with what is usual.
    else:
        if carcass.looks_like_a_carcass(parts):
            box = carcass.measure(parts, overall, _axes)
            placements.update(
                carcass.place(parts, box, overall, _axes, spec_dims.box_extent)
            )
        elif seating.looks_like_seating(parts):
            chair = seating.measure(parts, _named(spec), _axes,
                                    spec_dims.box_extent)
            placements.update(
                seating.place(parts, chair, spec_dims.box_extent)
            )

    for part in parts:
        # Already assembled as part of the box.
        if (part["id"], 0) in placements:
            continue
        pid = part["id"].lower()
        role = part.get("role", "")
        axes = _axes(part)
        count = max(1, int(part.get("count", 1)))

        # --- castors and feet: on the floor, spread round the base ---------
        if "castor" in pid or "wheel" in pid or "glide" in pid or "foot" in pid:
            diameter = axes.get("diameter", 50.0)
            # Under the legs, on the frame line - a glide is fixed to the
            # bottom of a leg, so any other position is simply wrong. It was
            # placed at 0.38 of the footprint, which left four pads sitting on
            # the floor near the legs but not beneath them.
            leg = next((p for p in parts
                        if "leg" in p["id"].lower() or "post" in p["id"].lower()),
                       None)
            section = (_axes(leg).get("width") or 70.0) if leg else 70.0
            spread_x = max(section, overall["frame_x"] / 2 - section / 2)
            spread_y = max(section, overall["frame_y"] / 2 - section / 2)
            corners = [(-spread_x, -spread_y), (spread_x, -spread_y),
                       (-spread_x, spread_y), (spread_x, spread_y)]
            for i in range(count):
                x, y = corners[i % 4]
                # Cylinders are built upright; a wheel lies on its side.
                rotation = (90, 0, 0) if "castor" in pid or "wheel" in pid else (0, 0, 0)
                placements[(part["id"], i)] = ((x, y, diameter / 2), rotation)
            continue

        # --- columns and legs: vertical, rising from the floor --------------
        if "column" in pid or "leg" in pid or "post" in pid or "stem" in pid:
            height = axes.get("height") or axes.get("length") or total_height * 0.6
            if count == 1:
                placements[(part["id"], 0)] = ((0, 0, height / 2), (0, 0, 0))
            else:
                # At the corners of the frame, with the leg's own section taken
                # off so its *outer face* lands on the frame line rather than
                # its centre - that is what a maker's "134 cm" measures, and
                # half a leg's width of error is visible in a render.
                section_x = axes.get("width") or axes.get("thickness") or 70.0
                section_y = axes.get("depth") or section_x
                spread_x = max(section_x, overall["frame_x"] / 2 - section_x / 2)
                spread_y = max(section_y, overall["frame_y"] / 2 - section_y / 2)
                corners = [(-spread_x, -spread_y), (spread_x, -spread_y),
                           (-spread_x, spread_y), (spread_x, spread_y)]
                for i in range(count):
                    x, y = corners[i % 4]
                    placements[(part["id"], i)] = ((x, y, height / 2), (0, 0, 0))
            continue

        # --- horizontal members: aprons, rails, beams, stretchers ----------
        #
        # "apron" was missing from this list, so a table's aprons fell through
        # to the unplaced fallback and were laid out in a diagonal row beside
        # the table. Correct sizes, absurd positions - which is exactly the
        # failure a preview render is for.
        if any(w in pid for w in ("apron", "skirt", "rail", "beam", "cross",
                                  "stretcher", "brace", "support", "spreader")):
            member_height = axes.get("height", axes.get("thickness", 60.0))
            length = axes.get("length", 0.0)

            # An apron sits immediately under the top; a floor-level stretcher
            # does not. Name tells us which.
            low = any(w in pid for w in ("stretcher", "spreader", "beam", "foot"))
            top_thickness = overall["top_thickness"]
            z = (member_height / 2 if low
                 else total_height - top_thickness - member_height / 2)

            # "long" members run along X, "short" ones across Y. Without this
            # both pairs lie parallel and the frame never closes.
            runs_along_x = True
            if "short" in pid or "side" in pid or "end" in pid:
                runs_along_x = False
            elif "long" in pid or "front" in pid or "back" in pid:
                runs_along_x = True
            elif length and length < overall.get("width", 0) * 0.75:
                runs_along_x = False

            # An apron is set on the *frame* line, not the top's edge, and only
            # by half its own thickness - so its outer face is flush with the
            # legs. The old code offset it by a whole member height from the
            # footprint edge, which on this table put both long aprons 85mm
            # from the centre: completely hidden underneath the top, which is
            # what "the table is inside the sheet" was describing.
            thickness = axes.get("thickness") or axes.get("depth") or 25.0
            for i in range(count):
                if runs_along_x:
                    offset = max(thickness, overall["frame_y"] / 2 - thickness / 2)
                    y = -offset if i % 2 == 0 else offset
                    placements[(part["id"], i)] = ((0, y, z), (0, 0, 0))
                else:
                    offset = max(thickness, overall["frame_x"] / 2 - thickness / 2)
                    x = -offset if i % 2 == 0 else offset
                    # Rotate about Z: members are built with their length on X.
                    placements[(part["id"], i)] = ((x, 0, z), (0, 0, 90))
            continue

        # --- drawers, doors and panels -------------------------------------
        #
        # A finer decomposition means more part names to recognise. Anything
        # the conventions below miss lands in the fallback row beside the
        # object - visible and correctly sized, but obviously not assembled.
        if any(w in pid for w in ("drawer", "door", "back.panel", "back_panel",
                                  "panel.back", "modesty", "splat", "plinth",
                                  "carcass", "side.panel", "arm")):
            axes_v = axes
            thickness = axes_v.get("thickness", 18.0)
            height = axes_v.get("height", 150.0)
            width = axes_v.get("width") or axes_v.get("length", 400.0)

            if "drawer" in pid or "door" in pid:
                # On the front face, stacked upward for multiple units.
                y = -(overall.get("depth", 400.0) / 2)
                for i in range(count):
                    z = total_height * 0.45 + i * (height + 20)
                    if "front" in pid or "door" in pid:
                        placements[(part["id"], i)] = ((0, y, z), (0, 0, 0))
                    elif "side" in pid:
                        offset = width / 2
                        x = -offset if i % 2 == 0 else offset
                        placements[(part["id"], i)] = ((x, 0, z), (0, 0, 90))
                    elif "base" in pid or "bottom" in pid:
                        placements[(part["id"], i)] = ((0, 0, z - height / 2), (0, 0, 0))
                    elif "back" in pid:
                        placements[(part["id"], i)] = (
                            (0, overall.get("depth", 400.0) / 2 - thickness, z),
                            (0, 0, 0))
                    else:
                        placements[(part["id"], i)] = ((0, y, z), (0, 0, 0))
                continue

            if "back" in pid or "modesty" in pid or "splat" in pid:
                # A vertical panel across the back.
                for i in range(count):
                    placements[(part["id"], i)] = (
                        (0, overall.get("depth", 400.0) / 2 - thickness / 2,
                         total_height * 0.55), (90, 0, 0))
                continue

            if "plinth" in pid:
                for i in range(count):
                    placements[(part["id"], i)] = ((0, 0, height / 2), (0, 0, 0))
                continue

            # Carcass / arm / side panels: upright at the sides.
            offset = (overall.get("width", 500.0) / 2) - thickness / 2
            for i in range(count):
                x = -offset if i % 2 == 0 else offset
                placements[(part["id"], i)] = (
                    (x, 0, total_height / 2), (0, 0, 90))
            continue

        # --- handles, knobs and pulls ---------------------------------------
        if any(w in pid for w in ("handle", "knob", "pull")):
            for i in range(count):
                z = total_height * 0.45 + i * 180
                placements[(part["id"], i)] = (
                    (0, -(overall.get("depth", 400.0) / 2) - 15, z), (0, 0, 0))
            continue

        # --- cushions: seat on the deck, back upright -----------------------
        if role == "upholstery" or "cushion" in pid:
            a = axes
            thickness = a.get("height", a.get("thickness", 120.0))
            width = a.get("width") or a.get("length", 500.0)
            for i in range(count):
                span = (count - 1) / 2
                x = (i - span) * (width + 12)
                if "back" in pid:
                    placements[(part["id"], i)] = (
                        (x, overall.get("depth", 900.0) / 2 - thickness,
                         total_height * 0.62), (0, 0, 0))
                else:
                    placements[(part["id"], i)] = (
                        (x, 0, total_height * 0.42), (0, 0, 0))
            continue

        # --- surfaces: a top sits at the object's full height ---------------
        # Reached only after the specific rules above have declined, so a
        # bare `role == "surface"` here is safe: drawer fronts, doors and back
        # panels have already been claimed.
        if (any(w in pid for w in ("top", "shelf", "worktop", "tabletop",
                                   "seat.panel", "lid"))
                or role == "surface"):
            thickness = axes.get("thickness", 18.0)
            for i in range(count):
                z = total_height - thickness / 2 - (i * (thickness + 300))
                placements[(part["id"], i)] = ((0, 0, z), (0, 0, 0))
            continue

        unplaced.append(part)

    # A part no rule recognised, but that the model did place, goes where the
    # model saw it. This is the case the row below was invented for - a name
    # none of the conventions above anticipated - and an observed position is
    # better than the shelf of orphans by a wide margin. The confidence floor
    # still applies: a guessed position inside the object is worse than an
    # honest one beside it.
    if not trust_the_model and trust_placement is not False:
        still_unplaced = []
        box = {"x_span": overall["x_span"], "y_span": overall["y_span"],
               "height": total_height}
        for part in unplaced:
            placement = spec_schema.placement_of(part)
            if placement is None or placement["confidence"] < \
                    spec_schema.PLACEMENT_CONFIDENCE_FLOOR:
                still_unplaced.append(part)
                continue
            extent = spec_dims.box_extent(part)
            for index, pose in anchors.resolve(part, placement, extent, box).items():
                placements[(part["id"], index)] = pose
        unplaced = still_unplaced

    # Anything without a convention is laid out in a row beside the object,
    # clearly separated. Visible and selectable beats buried inside the model
    # at a guessed position.
    offset_x = overall.get("width", 500.0) * 0.5 + 250.0
    for part in unplaced:
        axes = _axes(part)
        span = max(axes.values(), default=100.0)
        for i in range(max(1, int(part.get("count", 1)))):
            placements[(part["id"], i)] = (
                (offset_x + i * (span + 60), overall.get("depth", 350.0) + 300.0,
                 span / 2),
                (0, 0, 0),
            )
        offset_x += span + 200.0

    return placements
