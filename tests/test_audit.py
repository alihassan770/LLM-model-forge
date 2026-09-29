"""Deterministic geometry checks.

The value of these is entirely in their signal-to-noise. A check that fires on
a correct model gets switched off within a week, so most of what follows is
about the things that must NOT be reported: overhangs, cantilevers, castors
below the floor line, and parts that touch only one neighbour.
"""

from __future__ import annotations

import math

from forge.build import audit


def measurement(name: str, value: float, unit: str = "mm") -> dict:
    return {"name": name, "value": value, "unit": unit,
            "source": "user", "confidence": 1.0, "note": ""}


def part(pid: str, dimensions, primitive: str = "box", count: int = 1) -> dict:
    return {"id": pid, "display_name": pid, "parent_id": "",
            "role": "structural", "primitive": primitive, "material": "oak",
            "count": count, "dimensions": dimensions, "notes": ""}


def table_spec() -> dict:
    """A correct 1500 x 900 x 750 table: top, four legs, two aprons."""
    return {
        "object": {"category": "table"},
        "overall": [measurement("length", 1500), measurement("width", 900),
                    measurement("height", 750)],
        "parts": [
            part("top", [measurement("length", 1500), measurement("depth", 900),
                         measurement("thickness", 30)]),
            part("leg", [measurement("width", 70), measurement("depth", 70),
                         measurement("height", 720)], count=4),
            part("apron.long", [measurement("length", 1300),
                                measurement("height", 90),
                                measurement("thickness", 25)], count=2),
        ],
        "joints": [], "missing_dimensions": [], "assumptions": [],
        "readability": {"notes": "", "labels_found": True},
    }


# --------------------------------------------------------------------------- #
# The box arithmetic
# --------------------------------------------------------------------------- #

def test_an_unrotated_part_occupies_exactly_its_own_extent():
    lo, hi = audit.aabb((0.0, 0.0, 100.0), (0.0, 0.0, 0.0), (200.0, 40.0, 20.0))
    assert [round(v, 6) for v in lo] == [-100.0, -20.0, 90.0]
    assert [round(v, 6) for v in hi] == [100.0, 20.0, 110.0]


def test_a_quarter_turn_swaps_the_axes_a_part_occupies():
    lo, hi = audit.aabb((0.0, 0.0, 0.0), (0.0, 0.0, 90.0), (200.0, 40.0, 20.0))
    assert round(hi[0] - lo[0], 6) == 40.0
    assert round(hi[1] - lo[1], 6) == 200.0


def test_a_leaning_part_is_measured_where_it_actually_sits():
    """A raked back is wider front-to-back than its own thickness.

    Swapping axes to the nearest right angle would miss this entirely, which
    is why the eight corners are transformed rather than the extent permuted.
    """
    lo, hi = audit.aabb((0.0, 0.0, 500.0), (-30.0, 0.0, 0.0),
                        (400.0, 50.0, 600.0))
    depth = hi[1] - lo[1]
    expected = 600.0 * math.sin(math.radians(30)) + 50.0 * math.cos(
        math.radians(30))
    assert math.isclose(depth, expected, abs_tol=0.5)


# --------------------------------------------------------------------------- #
# A correct model must stay silent
# --------------------------------------------------------------------------- #

def test_a_correct_table_reports_nothing():
    assert audit.inspect(table_spec()) == []
    assert audit.summarise([]) == "geometry checks passed"


def test_an_overhanging_top_is_not_called_stray():
    """A top overhangs its frame by design; that is what a top does."""
    spec = table_spec()
    findings = audit.inspect(spec)
    assert [f for f in findings if f["kind"] == "stray_part"] == []


def test_a_single_part_model_is_not_detached_from_itself():
    spec = {
        "object": {"category": "shelf"},
        "overall": [measurement("length", 800), measurement("depth", 250),
                    measurement("height", 30)],
        "parts": [part("shelf", [measurement("length", 800),
                                 measurement("depth", 250),
                                 measurement("thickness", 30)])],
        "joints": [], "missing_dimensions": [], "assumptions": [],
        "readability": {"notes": "", "labels_found": True},
    }
    assert [f for f in audit.inspect(spec) if f["kind"] == "detached"] == []


# --------------------------------------------------------------------------- #
# The faults that shipped
# --------------------------------------------------------------------------- #

def test_an_object_assembled_to_the_wrong_size_is_an_error():
    """The Oakley sideboard: stated 450 deep, assembled 1022.

    One of the two numbers is wrong and the file should not go out quietly
    either way. This is the check that would have caught it before anyone
    opened the model and found a heap of boards.
    """
    spec = table_spec()
    # State a depth a third of what the parts actually build to.
    spec["overall"] = [measurement("length", 1500), measurement("width", 300),
                       measurement("height", 750)]
    findings = audit.inspect(spec)
    mismatches = [f for f in findings if f["kind"] == "envelope_mismatch"]
    assert mismatches, "an object built three times its stated depth must report"
    assert all(f["severity"] == "error" for f in mismatches)
    assert "depth" in " ".join(f["detail"] for f in mismatches)


def test_a_part_with_no_shape_is_reported_before_it_is_guessed():
    spec = table_spec()
    spec["parts"].append(part("bracket", [measurement("length", 60)]))
    findings = audit.inspect(spec)
    under = [f for f in findings if f["kind"] == "under_specified"]
    assert len(under) == 1
    assert under[0]["part_id"] == "bracket"
    assert under[0]["severity"] == "error"


def test_a_cylinder_needs_two_measurements_not_three():
    spec = table_spec()
    spec["parts"].append(part("pin", [measurement("diameter", 8),
                                      measurement("length", 30)],
                              primitive="cylinder"))
    assert [f for f in audit.inspect(spec)
            if f["kind"] == "under_specified"] == []


def test_a_part_floating_on_its_own_is_reported():
    """The exploded sideboard, and the row of orphans beside a model."""
    spec = table_spec()
    spec["parts"].append(
        part("gallery.fretwork.pierced",
             [measurement("length", 200), measurement("depth", 40),
              measurement("thickness", 10)]))
    findings = audit.inspect(spec)
    detached = [f for f in findings if f["kind"] == "detached"]
    assert [f["part_id"] for f in detached] == ["gallery.fretwork.pierced"]


def test_a_part_sunk_through_the_floor_is_reported():
    """`align_z: center` at anchor 0 buries a leg half its length."""
    spec = table_spec()
    for p in spec["parts"]:
        if p["id"] == "leg":
            p["placement"] = {
                "anchor_x": 0.05, "anchor_y": 0.05, "anchor_z": 0.5,
                "align_x": "center", "align_y": "center", "align_z": "center",
                "run_axis": "z", "tilt_x": 0.0, "tilt_y": 0.0, "tilt_z": 0.0,
                "pattern": "mirror_xy", "spacing_mm": 0.0, "confidence": 0.9,
            }
    # Placed by hand so the check is tested, not the placement rules.
    placements = {("top", 0): ((0.0, 0.0, 735.0), (0.0, 0.0, 0.0)),
                  ("leg", 0): ((-700.0, -400.0, -50.0), (0.0, 0.0, 0.0))}
    findings = audit.inspect(spec, placements)
    below = [f for f in findings if f["kind"] == "below_floor"]
    assert [f["part_id"] for f in below] == ["leg"]
    assert "below the floor" in below[0]["detail"]


def test_a_castor_resting_at_zero_is_not_called_sunk():
    """A part touching the floor is doing the right thing."""
    spec = table_spec()
    placements = {("top", 0): ((0.0, 0.0, 735.0), (0.0, 0.0, 0.0)),
                  ("leg", 0): ((-700.0, -400.0, 360.0), (0.0, 0.0, 0.0))}
    assert [f for f in audit.inspect(spec, placements)
            if f["kind"] == "below_floor"] == []


def test_a_specification_with_no_parts_says_so_plainly():
    findings = audit.inspect({"parts": [], "overall": []})
    assert len(findings) == 1
    assert findings[0]["kind"] == "no_parts"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def test_errors_are_reported_before_warnings():
    """The first line read should be the thing most likely to have ruined it."""
    spec = table_spec()
    spec["overall"] = [measurement("length", 1500), measurement("width", 300),
                       measurement("height", 750)]
    spec["parts"].append(part("bracket", [measurement("length", 60)]))
    findings = audit.inspect(spec)
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=lambda s: {"error": 0}.get(s, 1))
    assert findings[0]["severity"] == "error"


def test_the_summary_counts_both_kinds():
    findings = [{"severity": "error"}, {"severity": "warning"},
                {"severity": "warning"}]
    assert audit.summarise(findings) == "geometry checks: 1 error(s), 2 warning(s)"


# --------------------------------------------------------------------------- #
# Choosing between assemblies by measurement rather than by threshold
# --------------------------------------------------------------------------- #

def placement(**over) -> dict:
    full = {"anchor_x": 0.5, "anchor_y": 0.5, "anchor_z": 0.5,
            "align_x": "center", "align_y": "center", "align_z": "center",
            "run_axis": "", "tilt_x": 0.0, "tilt_y": 0.0, "tilt_z": 0.0,
            "pattern": "single", "spacing_mm": 0.0, "confidence": 0.9}
    full.update(over)
    return full


def test_errors_weigh_more_than_warnings():
    assert audit.score([]) == 0
    assert audit.score([{"severity": "warning"}]) == 1
    assert audit.score([{"severity": "error"}]) == 3


def test_a_clean_model_is_not_reassembled_a_second_time():
    """No point trying an alternative to something with nothing wrong with it."""
    strategy, _, findings = audit.best_placements(table_spec())
    assert strategy == "automatic"
    assert findings == []


def test_a_spec_with_no_placement_keeps_the_conventions():
    spec = table_spec()
    spec["overall"] = [measurement("length", 1500), measurement("width", 300),
                       measurement("height", 750)]
    strategy, _, findings = audit.best_placements(spec)
    assert strategy == "automatic"
    assert findings          # it is still reported as wrong


def test_the_better_measuring_assembly_wins():
    """The Oakley sideboard sat at 68% and took the worse of the two paths.

    A threshold cannot know which assembly is better; a comparison against the
    object's own stated dimensions can, so both are built and measured.
    """
    spec = table_spec()
    # A model that placed every part correctly...
    for p in spec["parts"]:
        if p["id"] == "top":
            p["placement"] = placement(anchor_z=1.0, align_z="max",
                                       run_axis="x", confidence=0.3)
        elif p["id"] == "leg":
            p["placement"] = placement(anchor_x=0.0, anchor_y=0.0, anchor_z=0.0,
                                       align_x="min", align_y="min",
                                       align_z="min", run_axis="z",
                                       pattern="mirror_xy", confidence=0.3)
        else:
            p["placement"] = placement(anchor_y=0.05, anchor_z=0.95,
                                       align_z="max", run_axis="x",
                                       pattern="mirror_y", confidence=0.3)
    # ...but below the confidence floor, so the automatic path ignores it.
    # Break the conventions by stating a footprint they will assemble wrongly.
    from forge.build import layout
    conventional = audit.inspect(spec, layout.place(spec))
    model_placed = audit.inspect(spec, layout.place(spec, trust_placement=True))

    # Whichever is better, best_placements must return that one.
    strategy, _, findings = audit.best_placements(spec)
    assert audit.score(findings) == min(audit.score(conventional),
                                        audit.score(model_placed))
    if audit.score(model_placed) < audit.score(conventional):
        assert strategy == "model-placed"


def test_a_tie_keeps_the_older_better_tested_path():
    spec = table_spec()
    for p in spec["parts"]:
        p["placement"] = placement(confidence=0.1)
    spec["overall"] = [measurement("length", 1500), measurement("width", 300),
                       measurement("height", 750)]
    strategy, _, _ = audit.best_placements(spec)
    # Both assemblies are wrong in the same way; the conventions keep the job.
    assert strategy in ("automatic", "model-placed")
    if strategy == "model-placed":
        # only ever chosen on a strict improvement
        from forge.build import layout
        a = audit.score(audit.inspect(spec, layout.place(spec)))
        b = audit.score(audit.inspect(spec, layout.place(spec, trust_placement=True)))
        assert b < a


# --------------------------------------------------------------------------- #
# The coordinate grid the model reads regions off
# --------------------------------------------------------------------------- #

def _png(width: int, height: int) -> bytes:
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 235, 230)).save(buffer, "PNG")
    return buffer.getvalue()


def test_the_grid_is_drawn_without_changing_the_image_size():
    """A region is a fraction of the picture, so the picture must not move."""
    import io

    from PIL import Image
    from forge.ingest import grid

    original = _png(854, 1280)
    annotated = grid.annotate(original)
    assert annotated != original
    assert Image.open(io.BytesIO(annotated)).size == (854, 1280)


def test_the_grid_survives_an_image_it_cannot_read():
    """A missing grid costs accuracy; a raised exception costs the run."""
    from forge.ingest import grid

    assert grid.annotate(b"not an image") == b"not an image"
    assert grid.annotate(b"") == b""


def test_a_tiny_image_is_left_alone():
    """Gridlines on a thumbnail would cover the thing being measured."""
    from forge.ingest import grid

    tiny = _png(40, 40)
    assert grid.annotate(tiny) == tiny


def test_the_gridlines_are_a_colour_no_product_photograph_contains():
    """So a line is never mistaken for an edge of the object."""
    from forge.ingest import grid

    assert grid.LINE == (255, 0, 255)


# --------------------------------------------------------------------------- #
# Artwork must not be stretched to fill its panel
# --------------------------------------------------------------------------- #

def _artwork_part(width, depth, height):
    return {"id": "crown", "primitive": "box", "count": 1,
            "appearance": {"hex": "#F2EFE9", "finish": "matte",
                           "source": "observed", "note": ""},
            "dimensions": [
                {"name": "width", "value": width, "unit": "mm",
                 "source": "user", "confidence": 1.0, "note": ""},
                {"name": "depth", "value": depth, "unit": "mm",
                 "source": "user", "confidence": 1.0, "note": ""},
                {"name": "height", "value": height, "unit": "mm",
                 "source": "user", "confidence": 1.0, "note": ""}]}


def test_a_crop_is_padded_to_the_shape_of_the_face_it_prints_onto():
    """The Dove wordmark was stretched four and a half times sideways.

    A roughly square 118 x 92 crop projected across a 900 x 160 crown is
    distorted by exactly the difference in their proportions - which is the
    smeared lettering. Padding the crop to the panel's shape first means the
    projection has nothing left to distort.
    """
    import io

    from PIL import Image
    from forge.jobs.runner import _fit_to_face

    crop = _png(118, 92)
    part = _artwork_part(900, 60, 160)          # a 5.6:1 crown
    fitted = Image.open(io.BytesIO(_fit_to_face(crop, part, "front")))
    assert math.isclose(fitted.width / fitted.height, 900 / 160, rel_tol=0.02)
    # Padded, never cropped: the artwork itself must survive intact.
    assert fitted.width >= 118 and fitted.height >= 92


def test_a_crop_already_the_right_shape_is_left_alone():
    from forge.jobs.runner import _fit_to_face

    crop = _png(400, 200)
    part = _artwork_part(800, 60, 400)          # also 2:1
    assert _fit_to_face(crop, part, "front") == crop


def test_a_tall_face_is_padded_top_and_bottom():
    import io

    from PIL import Image
    from forge.jobs.runner import _fit_to_face

    crop = _png(400, 100)
    part = _artwork_part(300, 60, 600)          # 0.5:1, much taller
    fitted = Image.open(io.BytesIO(_fit_to_face(crop, part, "front")))
    assert fitted.width == 400
    assert math.isclose(fitted.width / fitted.height, 0.5, rel_tol=0.02)


def test_padding_never_fails_the_build():
    from forge.jobs.runner import _fit_to_face

    part = _artwork_part(900, 60, 160)
    assert _fit_to_face(b"not an image", part, "front") == b"not an image"
    assert _fit_to_face(_png(10, 10), part, "nonsense") == _png(10, 10)
