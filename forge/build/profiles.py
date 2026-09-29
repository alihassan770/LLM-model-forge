"""Shaping a built primitive. Runs INSIDE Blender.

A box and a cylinder are the right starting point for almost every part, and
the wrong finishing point for most of them. Real furniture tapers, and its
edges are eased - a dining table with perfectly square arrises and parallel
legs reads as a packing crate however accurate its dimensions are. These three
operations are what make correct measurements look like the photograph.

Each one is applied to a part that is already built and already the right size,
and each one leaves the bounding box alone. That matters: the bill of materials
measures the part from the specification, not from the mesh, so anything here
that changed the overall size would put the cutting list and the model back
into the disagreement the shared `dims` module exists to prevent. A taper
removes material from within the envelope; it never grows it.
"""

from __future__ import annotations

import math

import bmesh
import bpy

MM = 0.001

# Below this a taper is a rounding error in the model's answer rather than a
# shape, and re-meshing the object to apply it is wasted work.
TAPER_EPSILON = 0.01


def longest_local_axis(extent: tuple[float, float, float]) -> int:
    x, y, z = extent
    if z >= x and z >= y:
        return 2
    return 0 if x >= y else 1


def apply_taper(obj, extent: tuple[float, float, float], taper: float) -> None:
    """Narrow one end of the part to `taper` of the other.

    The narrow end is the negative end of the part's longest local axis. For
    anything standing up that is the foot, which is where furniture tapers -
    a leg is thick where it carries the frame and thin where it meets the
    floor. The same convention on a horizontal member narrows one end of it,
    which is what an arm or a bracket does.

    Implemented by moving vertices rather than by scaling the object, because
    scaling would change the part's stated size and this must not.
    """
    if abs(taper - 1.0) < TAPER_EPSILON:
        return

    axis = longest_local_axis(extent)
    mesh = obj.data

    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        coords = [v.co[axis] for v in bm.verts]
        if not coords:
            return
        low, high = min(coords), max(coords)
        span = high - low
        if span <= 1e-9:
            return

        others = [i for i in (0, 1, 2) if i != axis]
        for vert in bm.verts:
            # 0 at the narrow end, 1 at the full-size end.
            along = (vert.co[axis] - low) / span
            factor = taper + (1.0 - taper) * along
            for other in others:
                vert.co[other] *= factor

        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()


def apply_edge_radius(obj, extent: tuple[float, float, float],
                      radius_mm: float) -> None:
    """Ease the part's edges with a bevel.

    Left as a live modifier, like the cushion's softness and the tube's wall:
    the file is meant to be edited, and an eased edge is exactly the kind of
    detail a designer changes. It is also reversible, which an applied bevel
    on a part whose radius was misread would not be.

    The width is clamped to a fraction of the part's smallest side. A 20mm
    round on a 12mm panel is not a rounded panel, it is a mesh that folds
    through itself, and Blender will happily produce that.
    """
    if radius_mm <= 0:
        return

    smallest = min(v for v in extent if v > 0) if any(extent) else 0.0
    if smallest <= 0:
        return
    # Just under half, so a fully rounded edge is reachable and a degenerate
    # one is not.
    radius = min(radius_mm, smallest * 0.48)

    modifier = obj.modifiers.new(name="Eased edges", type="BEVEL")
    modifier.width = radius * MM
    modifier.segments = 3 if radius > 1.0 else 2
    modifier.limit_method = "ANGLE"
    modifier.angle_limit = math.radians(30.0)
    # Without this a bevel on a panel whose faces nearly meet produces spikes
    # where three edges converge.
    modifier.harden_normals = False
    modifier.miter_outer = "MITER_ARC"


# Which two world axes each drawing plane maps onto, and which axis is left
# over for the section's thickness.
_PLANE_AXES = {
    "xz": (0, 2, 1),
    "yz": (1, 2, 0),
    "xy": (0, 1, 2),
}


def make_swept(name: str, path: list[dict], plane: str, closed: bool,
               extent: tuple[float, float, float], section: tuple[float, float],
               round_section: bool, resolution: int = 12):
    """A section carried along a curve - the primitive furniture actually needs.

    An armrest on a real chair is a bent tube that rises from the seat frame,
    turns forward and comes back: a loop with a hole in it. Every primitive
    before this one could only approximate that as a straight post and a pad,
    which is why the chair arms never looked like the drawing however accurate
    their dimensions were. The same shape covers a cabriole leg, a sled base, a
    bentwood back and a hairpin leg - none of which a box or a cylinder can
    express at all.

    The curve is drawn flat, in the plane the model names, and scaled into the
    part's measured bounding box. That keeps two promises at once: the shape
    comes from the drawing, and the size comes from the measurements, so a
    swept part still occupies exactly the envelope the specification states.

    Blender does the sweep with a bevelled curve rather than by building the
    mesh by hand - `bevel_depth` for a round section, a rectangle for a square
    one - which handles the mitring at every corner correctly and is far less
    code than lofting rings along a path.
    """
    u_axis, v_axis, thickness_axis = _PLANE_AXES.get(plane, _PLANE_AXES["xz"])

    # The drawing is normalised 0-1; the part's own extent says how big it is.
    u_size = extent[u_axis]
    v_size = extent[v_axis]

    curve = bpy.data.curves.new(f"{name}_path", type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = max(4, resolution)

    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(len(path) - 1)
    for point, source in zip(spline.bezier_points, path):
        position = [0.0, 0.0, 0.0]
        # Centred on the origin like every other primitive, so placement and
        # alignment need no special case for a swept part.
        position[u_axis] = (source["u"] - 0.5) * u_size * MM
        position[v_axis] = (source["v"] - 0.5) * v_size * MM
        position[thickness_axis] = 0.0
        point.co = position
        # Automatic handles give the smooth fair curve a bent tube actually
        # has. Vector handles would reproduce the polyline the model sent,
        # corners and all, which is not what a steam-bent arm looks like.
        point.handle_left_type = point.handle_right_type = "AUTO"

    spline.use_cyclic_u = bool(closed)

    width, thickness = section
    bevel_object = None
    if round_section:
        curve.bevel_depth = max(width, thickness) / 2.0 * MM
        curve.bevel_resolution = 4
    else:
        curve.bevel_mode = "OBJECT"
        bevel_object = _rectangle_bevel(f"{name}_section", width, thickness)
        curve.bevel_object = bevel_object
    # Caps matter: an open sweep without them exports as a tube you can see
    # through, which reads as a modelling error in every viewer.
    curve.use_fill_caps = True

    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)

    # Converted to a mesh because everything downstream - the exporters, the
    # preview, the audit's bounding boxes - works on meshes, and a curve
    # object would silently vanish from several of them.
    previous = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.convert(target="MESH")
    converted = bpy.context.view_layer.objects.active
    converted.name = name

    # The section is scaffolding and the mesh no longer refers to it. Left
    # behind it appears in the outliner beside the real parts, so a designer
    # opening a chair with two swept arms finds two mystery curves called
    # `arm.loop_1_section` among the things they can select. Hiding them is not
    # enough - the promise is that every object in the file is a part of the
    # object.
    if bevel_object is not None:
        bpy.data.objects.remove(bevel_object, do_unlink=True)

    if previous is not None:
        bpy.context.view_layer.objects.active = previous
    return converted


def _rectangle_bevel(name: str, width: float, thickness: float):
    """A closed rectangle used as the section of a square-profile sweep."""
    curve = bpy.data.curves.new(name, type="CURVE")
    curve.dimensions = "2D"
    spline = curve.splines.new("POLY")
    spline.points.add(3)
    half_w = width / 2.0 * MM
    half_t = thickness / 2.0 * MM
    for point, (x, y) in zip(spline.points,
                             [(-half_w, -half_t), (half_w, -half_t),
                              (half_w, half_t), (-half_w, half_t)]):
        point.co = (x, y, 0.0, 1.0)
    spline.use_cyclic_u = True

    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    # Hidden: it is a construction object, not a part of the model, and it
    # must not appear in the outliner as something a designer can select.
    obj.hide_viewport = True
    obj.hide_render = True
    return obj


def make_revolved(name: str, profile: list[dict], diameter_mm: float,
                  length_mm: float, segments: int = 48):
    """A turned part, lathed from its silhouette.

    This is what `revolve` was always supposed to mean. Without a profile the
    builder approximates a turning with a tapered cylinder and says so in the
    object's properties; given four or five points from the model it can
    produce the actual shape - the swell of a cabriole leg, the neck of a
    pedestal, the bead under a knob.

    `at` runs 0 to 1 from the bottom of the part to the top, and `scale`
    multiplies the stated radius. The profile is closed onto the axis at both
    ends so the result is a solid rather than a surface.
    """
    radius = diameter_mm / 2.0 * MM
    height = length_mm * MM

    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)

    bm = bmesh.new()
    try:
        # Centred on the origin like every other primitive here, so placement
        # and alignment treat it no differently.
        def z_at(fraction: float) -> float:
            return (fraction - 0.5) * height

        points = [(max(p["scale"] * radius, radius * 0.01), z_at(p["at"]))
                  for p in profile]

        # Close the silhouette onto the axis at each end. Without these the
        # spin produces an open tube and the exporters write a shell that
        # renders black from inside.
        points.insert(0, (0.0, points[0][1]))
        points.append((0.0, points[-1][1]))

        verts = [bm.verts.new((r, 0.0, z)) for r, z in points]
        edges = [bm.edges.new((verts[i], verts[i + 1]))
                 for i in range(len(verts) - 1)]

        bmesh.ops.spin(
            bm,
            geom=verts + edges,
            axis=(0.0, 0.0, 1.0),
            cent=(0.0, 0.0, 0.0),
            dvec=(0.0, 0.0, 0.0),
            angle=2.0 * math.pi,
            steps=max(8, segments),
            use_merge=False,
        )
        # The spin's last ring lands on its first. Merging them is what makes
        # the seam disappear and the normals agree across it.
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()

    return obj
