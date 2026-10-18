#!/usr/bin/env python3
"""vmf_shapes.py - brushes that aren't axis-aligned boxes, and displacement terrain.

vbsp only knows convex brushes, so every shape here is made of convex pieces;
a concave shape (a ring, an arch, a polyline wall) is split into pieces that
share their faces exactly.

  box       a box from mins/maxs or centre/size, optionally rotated (any angle)
  prism     any convex polygon extruded vertically (footprint in world x/y)
  wedge     a ramp / sloped block: a box whose top drops to one side
  cylinder  n-sided column between two points in any direction (posts, pipes, logs)
  cone      tapered column, cone or pyramid
  ring      hollow cylinder or a part of one (curved walls, tunnels, barrel vaults, tyres)
  arch      a wall with one or more arched openings (arcades, arched doors/windows)
  roof      gable / shed / barrel / flat roof over a footprint, with gable-end infill
  wall      a wall along a polyline, mitered at corners, with gaps (doorways)
  fence     a wall preset: thin, see-through material, posts at intervals
  terrain   displacement ground over a rectangle; height from hills, ridges,
            noise and a raised rim, flattened where asked

Every face plane is taken from three of that face's vertices, picked by a rule
that depends only on the set of points (not their order), so two pieces
sharing a face get exactly opposite planes: no cracks, no overlaps. Vertices
are rounded to 1/1000 unit (or snapped to `snap` units when given) before the
planes are made, after any rotation. Hammer++'s vertices_plus are computed
from the final planes, so the editor shows exactly what vbsp compiles.

Specs are dicts (the same objects the layout JSON's "shapes" list holds);
`make_pieces(spec)` turns one into convex pieces, `add_shape(geo, spec)` puts
it into a map.
"""

from __future__ import annotations

import json
import math
import sys

import vmf
from vmf import KV, Node
import vmf_build as vb

BuildError = vb.BuildError

NODRAW = "TOOLS/TOOLSNODRAW"

COMMON_KEYS = {"type", "name", "material", "materials", "entity", "detail", "kv", "snap", "yaw", "pitch",
               "roll", "pivot", "repeat", "comment", "tex_scale", "lightmapscale", "color", "outputs", "origin"}

SHAPE_KEYS = {
    "box": {"mins", "maxs", "center", "size"},
    "prism": {"points", "z", "height"},
    "wedge": {"mins", "maxs", "up", "low"},
    "cylinder": {"base", "height", "from", "to", "radius", "sides", "align"},
    "cone": {"base", "height", "from", "to", "radius", "top_radius", "sides", "align"},
    "ring": {"base", "height", "from", "to", "axis", "center", "length", "radius", "inner", "thickness",
             "arc", "sides"},
    "arch": {"from", "to", "z", "thickness", "openings", "opening", "segments", "align"},
    "roof": {"style", "mins", "maxs", "z", "axis", "up", "rise", "pitch", "overhang", "end_overhang",
             "thickness", "gables", "segments"},
    "wall": {"points", "z", "height", "thickness", "closed", "gaps", "align"},
    "fence": {"points", "z", "height", "thickness", "closed", "gaps", "align", "posts", "post_size",
              "post_material"},
    "railing": {"points", "z", "height", "closed", "gaps", "posts", "post_size", "post_material", "rails",
                "rail_size"},
    "terrain": {"mins", "maxs", "z", "tile", "power", "hills", "ridges", "noise", "edge", "flat", "clamp",
                "thickness", "alpha", "skip_flat", "flat_material", "side_material"},
}

DIR_VECS = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1),
            "-z": (0, 0, -1), "x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def length(a):
    return math.sqrt(dot(a, a))


def unit(a):
    ln = length(a)
    if ln < 1e-12:
        raise BuildError("zero-length direction")
    return (a[0] / ln, a[1] / ln, a[2] / ln)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def smoothstep(t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return t * t * (3 - 2 * t)


def basis(w):
    """(u, v) perpendicular to unit axis w. Vertical axis: u=+x, v=+y. Any other
    axis: u horizontal, v as close to +z as possible (so angle 90 = up)."""
    if abs(w[2]) > 0.999:
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0) if w[2] > 0 else (0.0, -1.0, 0.0)
    u = unit(cross(w, (0.0, 0.0, 1.0)))
    v = unit(cross(u, w))
    return u, v


def fmt_f(x):
    s = "%.6f" % x
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ---------------------------------------------------------------------------
# 2D convex hull / polygon helpers
# ---------------------------------------------------------------------------

def hull2d(pts, eps=1e-7):
    """Convex hull (counter-clockwise, collinear points dropped) of 2D points."""
    p = sorted(set((float(x), float(y)) for x, y in pts))
    if len(p) < 3:
        return p

    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for q in p:
        while len(lower) >= 2 and cr(lower[-2], lower[-1], q) <= eps:
            lower.pop()
        lower.append(q)
    upper = []
    for q in reversed(p):
        while len(upper) >= 2 and cr(upper[-2], upper[-1], q) <= eps:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


def area2d(poly):
    a = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return a / 2


# ---------------------------------------------------------------------------
# Pieces: a piece is a list of (polygon of 3D points, tag) faces of one convex
# brush. Tags pick materials ("top"/"bottom"/"side" by default, from the face
# normal; shapes may tag faces "inner", "outer", "end", "cap"...).
# ---------------------------------------------------------------------------

def prism(poly2d, to3d, d0, d1, cap_tags=("cap", "cap"), side_tag=None):
    """Faces of the convex hull of poly2d extruded from depth d0 to d1, where
    to3d(a, b, d) maps 2D coords + depth to a world point. None if the polygon
    is degenerate (area < 0.25 unit^2)."""
    poly = hull2d(poly2d)
    if len(poly) < 3 or abs(area2d(poly)) < 0.25 or abs(d1 - d0) < 0.01:
        return None
    lo = [to3d(a, b, d0) for a, b in poly]
    hi = [to3d(a, b, d1) for a, b in poly]
    faces = [(lo, cap_tags[0]), (hi, cap_tags[1])]
    n = len(poly)
    for i in range(n):
        j = (i + 1) % n
        faces.append(([lo[i], lo[j], hi[j], hi[i]], side_tag))
    return faces


def vertical_prism(poly_xy, z0, z1):
    return prism(poly_xy, lambda a, b, d: (a, b, d), z0, z1, cap_tags=(None, None))


def frame_mapper(origin, u, v, w):
    def to3d(a, b, d):
        return (origin[0] + u[0] * a + v[0] * b + w[0] * d,
                origin[1] + u[1] * a + v[1] * b + w[1] * d,
                origin[2] + u[2] * a + v[2] * b + w[2] * d)
    return to3d


# ---------------------------------------------------------------------------
# Solids from pieces
# ---------------------------------------------------------------------------

def _canon_triple(points):
    """Three well-spread points of a face, chosen only from the point SET (so
    the same face of two neighbouring brushes gets the same plane)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return None
    a = pts[0]
    b = max(pts, key=lambda p: (dot(sub(p, a), sub(p, a)), p))
    ab = sub(b, a)
    c = max(pts, key=lambda p: (length(cross(ab, sub(p, a))), p))
    if length(cross(ab, sub(c, a))) < 1e-6:
        return None
    return a, b, c


def tex_axes(n):
    """World-aligned texture axes like make_box; on a sloped face they are
    projected into the face so the texture isn't stretched."""
    dom = max(range(3), key=lambda i: abs(n[i]))
    u0, v0 = {2: ((1, 0, 0), (0, -1, 0)), 1: ((1, 0, 0), (0, 0, -1)), 0: ((0, 1, 0), (0, 0, -1))}[dom]
    if abs(abs(n[dom]) - 1.0) < 1e-6:
        return u0, v0
    u = unit(sub(u0, mul(n, dot(u0, n))))
    v = unit(cross(n, u))
    if dot(v, v0) < 0:
        v = mul(v, -1)
    return u, v


def auto_tag(n):
    if n[2] > 0.3:
        return "top"
    if n[2] < -0.3:
        return "bottom"
    return "side"


def pick_material(tag, auto, mats, default):
    for k in (tag, auto):
        if k and k in mats and mats[k]:
            return mats[k]
    return default


def _fmt_axis(vec, scale):
    return "[%s %s %s 0] %s" % (fmt_f(vec[0]), fmt_f(vec[1]), fmt_f(vec[2]), fmt_f(scale))


def make_solid(faces, alloc, material, mats=None, snapper=None, color="0 180 137", visgroup=None,
               lightmapscale="16", tex_scale=0.25):
    """A solid from one convex piece. Returns None if snapping collapsed it."""
    mats = mats or {}
    snapper = snapper or (lambda p: tuple(vb.num(c) for c in p))
    sfaces = [([snapper(p) for p in poly], tag) for poly, tag in faces]
    allp = [p for poly, _t in sfaces for p in poly]
    cen = tuple(sum(p[i] for p in allp) / len(allp) for i in range(3))
    solid = Node("solid", [KV("id", alloc.take())])
    seen = set()
    for poly, tag in sfaces:
        tri = _canon_triple(poly)
        if tri is None:
            continue
        a, b, c = tri
        nin = cross(sub(b, a), sub(c, a))      # (p1-p0) x (p2-p0) must point INTO the brush
        if dot(nin, sub(cen, a)) < 0:
            b, c = c, b
            nin = mul(nin, -1)
        n_out = unit(mul(nin, -1))
        key = (round(n_out[0], 5), round(n_out[1], 5), round(n_out[2], 5), round(dot(n_out, a), 2))
        if key in seen:
            continue
        seen.add(key)
        u, v = tex_axes(n_out)
        side = Node("side", [KV("id", alloc.take()),
                             KV("plane", "(%s) (%s) (%s)" % (vb.fmt_pt(a), vb.fmt_pt(b), vb.fmt_pt(c)))])
        side.add_kv("material", pick_material(tag, auto_tag(n_out), mats, material))
        side.add_kv("uaxis", _fmt_axis(u, tex_scale))
        side.add_kv("vaxis", _fmt_axis(v, tex_scale))
        side.add_kv("rotation", "0")
        side.add_kv("lightmapscale", str(lightmapscale))
        side.add_kv("smoothing_groups", "0")
        solid.items.append(side)
    wind = vmf.solid_windings(solid)
    if len(wind) < 4:
        return None
    live = {id(s) for s, _w in wind}
    solid.items = [it for it in solid.items if not (isinstance(it, Node) and it.name == "side" and id(it) not in live)]
    for s, w in wind:
        s.items.insert(2, Node("vertices_plus", [KV("v", vb.fmt_pt(p)) for p in w]))
    solid.items.append(vb.editor_block(color, visgroup))
    return solid


# ---------------------------------------------------------------------------
# Spec helpers
# ---------------------------------------------------------------------------

def _num(spec, key, default=None, where="shape"):
    v = spec.get(key, default)
    if v is None:
        raise BuildError(f"{where}: needs '{key}'")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise BuildError(f"{where}: '{key}' must be a number, got {v!r}")


def _vec(spec, key, n=3, where="shape", default=None):
    if key not in spec:
        if default is not None:
            return list(default)
        raise BuildError(f"{where}: needs '{key}'")
    return vb._vec(spec[key], n, f"{where}.{key}")


def _zrange(spec, where, z_default=None):
    """'z': [z0, z1] or 'z': z0 + 'height'."""
    z = spec.get("z", z_default)
    if z is None:
        raise BuildError(f"{where}: needs 'z' ([z0, z1], or z0 with 'height')")
    if isinstance(z, (list, tuple)):
        if len(z) != 2:
            raise BuildError(f"{where}: 'z' wants [z0, z1]")
        z0, z1 = float(z[0]), float(z[1])
    else:
        z0 = float(z)
        z1 = z0 + _num(spec, "height", where=where)
    if z1 <= z0:
        raise BuildError(f"{where}: z range {vb.num(z0)}..{vb.num(z1)} is empty")
    return z0, z1


def _axis_ends(spec, where):
    """(start, end) of a cylinder-like shape: from/to, or base + height (vertical)."""
    if "from" in spec and "to" in spec:
        a, b = _vec(spec, "from", 3, where), _vec(spec, "to", 3, where)
    elif "base" in spec:
        a = _vec(spec, "base", 3, where)
        b = [a[0], a[1], a[2] + _num(spec, "height", where=where)]
    elif "center" in spec and ("axis" in spec):
        c = _vec(spec, "center", 3, where)
        w = DIR_VECS.get(str(spec["axis"]).lower()) if isinstance(spec["axis"], str) else \
            tuple(vb._vec(spec["axis"], 3, where + ".axis"))
        if w is None:
            raise BuildError(f"{where}: 'axis' must be x, y, z or a vector")
        w = unit(w)
        half = _num(spec, "length", where=where) / 2
        a, b = list(sub(c, mul(w, half))), list(add(c, mul(w, half)))
    else:
        raise BuildError(f"{where}: needs 'from' + 'to', 'base' + 'height', or 'center' + 'axis' + 'length'")
    if length(sub(b, a)) < 0.5:
        raise BuildError(f"{where}: the axis is too short")
    return tuple(a), tuple(b)


def _sides(spec, radius, where, default_min=8):
    if "sides" in spec:
        n = int(spec["sides"])
        if n < 3:
            raise BuildError(f"{where}: 'sides' must be >= 3")
        return n
    n = int(round(radius / 6.0 / 4.0)) * 4
    return max(default_min, min(32, n))


def _start_angle(spec, n):
    """'flat' (default): a flat face points along +u (axis-aligned sides when n % 4 == 0)."""
    al = str(spec.get("align", "flat")).lower()
    return math.pi / n if al == "flat" else 0.0


# ---------------------------------------------------------------------------
# Shapes -> pieces (lists of faces, float points, before rotation / snapping)
# ---------------------------------------------------------------------------

def shape_box(spec, where):
    if "mins" in spec:
        mins = _vec(spec, "mins", 3, where)
        maxs = _vec(spec, "maxs", 3, where)
        lo = [min(mins[i], maxs[i]) for i in range(3)]
        hi = [max(mins[i], maxs[i]) for i in range(3)]
    else:
        c = _vec(spec, "center", 3, where)
        s = _vec(spec, "size", 3, where)
        lo = [c[i] - s[i] / 2 for i in range(3)]
        hi = [c[i] + s[i] / 2 for i in range(3)]
    p = vertical_prism([(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])], lo[2], hi[2])
    if p is None:
        raise BuildError(f"{where}: the box is flat")
    return [p]


def shape_prism(spec, where):
    pts = spec.get("points")
    if not isinstance(pts, list) or len(pts) < 3:
        raise BuildError(f"{where}: 'points' wants 3+ [x, y] footprint points")
    poly = [tuple(vb._vec(p, 2, f"{where}.points[{i}]")) for i, p in enumerate(pts)]
    z0, z1 = _zrange(spec, where)
    if len(hull2d(poly)) < len(set(poly)):
        raise BuildError(f"{where}: the footprint isn't convex (split it into convex prisms, or use 'wall')")
    p = vertical_prism(poly, z0, z1)
    if p is None:
        raise BuildError(f"{where}: degenerate footprint")
    return [p]


def shape_wedge(spec, where):
    mins, maxs = _vec(spec, "mins", 3, where), _vec(spec, "maxs", 3, where)
    lo = [min(mins[i], maxs[i]) for i in range(3)]
    hi = [max(mins[i], maxs[i]) for i in range(3)]
    up = str(spec.get("up", "+x")).lower()
    if up not in ("+x", "-x", "+y", "-y"):
        raise BuildError(f"{where}: 'up' (the direction the top rises toward) must be +x, -x, +y or -y")
    low = float(spec.get("low", 0))
    ax = 0 if up[1] == "x" else 1
    o = 1 - ax
    L = hi[ax] - lo[ax]
    z0, z1 = lo[2], hi[2]
    if low < 0 or low >= z1 - z0:
        raise BuildError(f"{where}: 'low' must be 0 <= low < height")
    poly = [(0, z0), (L, z0), (L, z1), (0, z0 + low)]

    def to3d(s, z, d):
        p = [0.0, 0.0, z]
        p[ax] = lo[ax] + s if up[0] == "+" else hi[ax] - s
        p[o] = d
        return tuple(p)
    pc = prism(poly, to3d, lo[o], hi[o], cap_tags=("side", "side"))
    if pc is None:
        raise BuildError(f"{where}: degenerate wedge")
    return [pc]


def wedge_slope_deg(spec):
    mins, maxs = spec["mins"], spec["maxs"]
    up = str(spec.get("up", "+x")).lower()
    ax = 0 if up[1] == "x" else 1
    L = abs(float(maxs[ax]) - float(mins[ax]))
    h = abs(float(maxs[2]) - float(mins[2])) - float(spec.get("low", 0))
    return math.degrees(math.atan2(h, L))


def shape_cylinder(spec, where):
    a, b = _axis_ends(spec, where)
    r = _num(spec, "radius", where=where)
    n = _sides(spec, r, where)
    w = unit(sub(b, a))
    u, v = basis(w)
    t0 = _start_angle(spec, n)
    poly = [(r * math.cos(t0 + 2 * math.pi * k / n), r * math.sin(t0 + 2 * math.pi * k / n)) for k in range(n)]
    pc = prism(poly, frame_mapper(a, u, v, w), 0.0, length(sub(b, a)))
    if pc is None:
        raise BuildError(f"{where}: degenerate cylinder")
    return [pc]


def shape_cone(spec, where):
    a, b = _axis_ends(spec, where)
    r0 = _num(spec, "radius", where=where)
    r1 = float(spec.get("top_radius", 0))
    n = _sides(spec, max(r0, r1), where)
    w = unit(sub(b, a))
    u, v = basis(w)
    h = length(sub(b, a))
    t0 = _start_angle(spec, n)
    m = frame_mapper(a, u, v, w)
    ring0 = [m(r0 * math.cos(t0 + 2 * math.pi * k / n), r0 * math.sin(t0 + 2 * math.pi * k / n), 0.0)
             for k in range(n)]
    faces = [(ring0, "cap")]
    if r1 > 0.25:
        ring1 = [m(r1 * math.cos(t0 + 2 * math.pi * k / n), r1 * math.sin(t0 + 2 * math.pi * k / n), h)
                 for k in range(n)]
        faces.append((ring1, "cap"))
        for k in range(n):
            j = (k + 1) % n
            faces.append(([ring0[k], ring0[j], ring1[j], ring1[k]], None))
    else:
        apex = m(0.0, 0.0, h)
        for k in range(n):
            faces.append(([ring0[k], ring0[(k + 1) % n], apex], None))
    return [faces]


def ring_angles(arc0, arc1, nseg):
    span = arc1 - arc0
    return [math.radians(arc0 + span * k / nseg) for k in range(nseg + 1)]


def ring_pieces(a, b, r_in, r_out, arc0, arc1, nseg, where):
    """Convex pieces of a hollow cylinder segment between axis points a, b:
    nseg segments from angle arc0 to arc1 (degrees, measured from basis u
    toward v). Curved faces are tagged 'outer' / 'inner', the rest 'end'."""
    if r_in < 0 or r_out <= r_in:
        raise BuildError(f"{where}: needs 0 <= inner < radius")
    span = arc1 - arc0
    if span <= 0 or span > 360 + 1e-9:
        raise BuildError(f"{where}: 'arc' wants [start, end] degrees with 0 < end-start <= 360")
    w = unit(sub(b, a))
    u, v = basis(w)
    m = frame_mapper(a, u, v, w)
    h = length(sub(b, a))
    angs = ring_angles(arc0, arc1, nseg)
    inner = [(r_in * math.cos(t), r_in * math.sin(t)) for t in angs]
    outer = [(r_out * math.cos(t), r_out * math.sin(t)) for t in angs]
    half = math.cos(math.radians(span / nseg) / 2)
    want = {"outer": r_out * half, "inner": r_in * half, "end": (r_in + r_out) / 2}
    pieces = []
    for k in range(nseg):
        poly = [inner[k], outer[k], outer[k + 1], inner[k + 1]] if r_in > 0.01 else [(0.0, 0.0), outer[k], outer[k + 1]]
        pc = prism(poly, m, 0.0, h, cap_tags=("end", "end"))
        if pc is None:
            continue
        for i in range(2, len(pc)):
            face = pc[i][0]
            rel = sub(lerp(face[0], face[1], 0.5), a)
            rad = length(sub(rel, mul(w, dot(rel, w))))
            tag = min(want, key=lambda t: abs(want[t] - rad))
            pc[i] = (face, tag)
        pieces.append(pc)
    return pieces


def shape_ring(spec, where):
    a, b = _axis_ends(spec, where)
    r = _num(spec, "radius", where=where)
    if "inner" in spec:
        ri = float(spec["inner"])
    else:
        ri = r - _num(spec, "thickness", where=where)
    arc = spec.get("arc", [0, 360])
    a0, a1 = float(arc[0]), float(arc[1])
    n = _sides(spec, r, where, default_min=12)
    nseg = max(1, int(math.ceil(n * (a1 - a0) / 360.0 - 1e-9)))
    return ring_pieces(a, b, ri, r, a0, a1, nseg, where)


def _wall_frame(spec, where):
    p0 = _vec(spec, "from", 2, where)
    p1 = _vec(spec, "to", 2, where)
    dvec = (p1[0] - p0[0], p1[1] - p0[1], 0.0)
    L = length(dvec)
    if L < 1:
        raise BuildError(f"{where}: 'from' and 'to' are the same point")
    d = unit(dvec)
    nrm = (-d[1], d[0], 0.0)
    return (p0[0], p0[1], 0.0), d, nrm, L


def arch_opening_pieces(c, hw, z_sill, height, rise, z1, segments):
    """2D (s, z) polygons filling the wall above an arched opening between
    s = c-hw .. c+hw, opening bottom z_sill, crown at z_sill + height."""
    zs = z_sill + height - rise                    # springline
    crown = zs + rise
    if crown > z1 + 0.01:
        raise BuildError(f"the arch crown (z {vb.num(crown)}) is above the wall top (z {vb.num(z1)})")
    if rise <= 0 or rise > hw + 1e-6:
        raise BuildError(f"arch 'rise' must be > 0 and <= half the width ({vb.num(hw)})")
    R = (hw * hw + rise * rise) / (2 * rise)
    cz = crown - R                                 # circle centre (c, cz)
    th0 = math.atan2(zs - cz, -hw)                 # left springing point angle
    th1 = math.atan2(zs - cz, hw)                  # right springing point angle
    n = max(2, int(segments))
    arcp = [(c + R * math.cos(th0 + (th1 - th0) * k / n), cz + R * math.sin(th0 + (th1 - th0) * k / n))
            for k in range(n + 1)]
    arcp[0] = (c - hw, zs)
    arcp[-1] = (c + hw, zs)
    xl, xr, zt = c - hw, c + hw, z1

    def boundary(p):
        """Where the ray from the circle centre through p leaves the box above the springline."""
        dx, dz = p[0] - c, p[1] - cz
        best = None
        for t, edge in (((xl - c) / dx if dx < -1e-9 else None, "l"),
                        ((xr - c) / dx if dx > 1e-9 else None, "r"),
                        ((zt - cz) / dz if dz > 1e-9 else None, "t")):
            if t is None or t < 1 - 1e-9:
                continue
            q = (c + dx * t, cz + dz * t)
            if xl - 1e-6 <= q[0] <= xr + 1e-6 and zs - 1e-6 <= q[1] <= zt + 1e-6 and (best is None or t < best[0]):
                best = (t, q, edge)
        if best is None:
            return p, "l" if dx < 0 else "r"
        return best[1], best[2]
    bnd = [boundary(p) for p in arcp]
    order = {"l": 0, "t": 1, "r": 2}
    corners = {("l", "t"): (xl, zt), ("t", "r"): (xr, zt), ("l", "r"): None}
    polys = []
    for k in range(n):
        (q0, e0), (q1, e1) = bnd[k], bnd[k + 1]
        poly = [arcp[k], arcp[k + 1], q1, q0]
        if order[e0] < order[e1]:
            if (e0, e1) == ("l", "r"):
                poly += [(xl, zt), (xr, zt)]
            else:
                poly.append(corners[(e0, e1)])
        polys.append(poly)
    return polys


def shape_arch(spec, where):
    origin, d, nrm, L = _wall_frame(spec, where)
    z0, z1 = _zrange(spec, where)
    t = _num(spec, "thickness", 16, where)
    align = str(spec.get("align", "center")).lower()
    off = {"center": -t / 2, "left": 0.0, "right": -t}.get(align)
    if off is None:
        raise BuildError(f"{where}: 'align' must be center, left or right")
    ops = spec.get("openings")
    if ops is None and "opening" in spec:
        ops = [spec["opening"]]
    if not ops:
        ops = [{}]
    segs = int(spec.get("segments", 8))
    opens = []
    for i, op in enumerate(ops):
        ow = f"{where}.openings[{i}]"
        width = _num(op, "width", 96, ow)
        sill = float(op.get("sill", 0))
        hw = width / 2
        c = float(op.get("at", L / 2))
        height = _num(op, "height", min(z1 - z0 - sill, hw + 96), ow)
        rise = float(op.get("rise", min(hw, height)))
        if c - hw < -1e-6 or c + hw > L + 1e-6:
            raise BuildError(f"{ow}: opening {vb.num(c - hw)}..{vb.num(c + hw)} doesn't fit the wall (0..{vb.num(L)})")
        if z0 + sill + height > z1 + 1e-6:
            raise BuildError(f"{ow}: sill {vb.num(sill)} + height {vb.num(height)} is taller than the wall")
        opens.append((c, hw, sill, height, rise, ow))
    opens.sort()
    polys = []
    s = 0.0
    for c, hw, sill, height, rise, ow in opens:
        if c - hw < s - 1e-6:
            raise BuildError(f"{ow}: overlaps the previous opening")
        if c - hw > s + 0.01:
            polys.append([(s, z0), (c - hw, z0), (c - hw, z1), (s, z1)])
        if sill > 0.01:
            polys.append([(c - hw, z0), (c + hw, z0), (c + hw, z0 + sill), (c - hw, z0 + sill)])
        try:
            polys += arch_opening_pieces(c, hw, z0 + sill, height, rise, z1, segs)
        except BuildError as e:
            raise BuildError(f"{ow}: {e}")
        s = c + hw
    if L - s > 0.01:
        polys.append([(s, z0), (L, z0), (L, z1), (s, z1)])

    def to3d(sv, zv, dv):
        return (origin[0] + d[0] * sv + nrm[0] * dv, origin[1] + d[1] * sv + nrm[1] * dv, zv)
    pieces = [pc for pc in (prism(p, to3d, off, off + t, cap_tags=("side", "side")) for p in polys) if pc]
    return pieces


def roof_pieces(style, x0, y0, x1, y1, z, axis, rise, overhang, end_over, thick, gables, segments,
                up=None, where="roof"):
    """[(piece, role)] with role 'roof' or 'gable' (gable-end infill)."""
    if axis not in ("x", "y"):
        raise BuildError(f"{where}: 'axis' (the ridge direction) must be x or y")
    ra = 0 if axis == "x" else 1            # ridge axis
    aa = 1 - ra                             # across axis
    lo, hi = (x0, y0), (x1, y1)
    H = (hi[aa] - lo[aa]) / 2
    cen = (lo[aa] + hi[aa]) / 2
    r0, r1 = lo[ra] - end_over, hi[ra] + end_over

    def to3d(u, zz, dd):
        p = [0.0, 0.0, zz]
        p[aa] = cen + u
        p[ra] = dd
        return tuple(p)
    out = []
    if style == "flat":
        pc = prism([(-H - overhang, z), (H + overhang, z), (H + overhang, z + thick), (-H - overhang, z + thick)],
                   to3d, r0, r1)
        out.append((pc, "roof"))
        return out
    if rise <= 0:
        raise BuildError(f"{where}: needs 'rise' > 0 (or 'pitch')")
    if style == "gable":
        s = rise / H
        T = thick * math.sqrt(1 + s * s)
        zr = z + rise
        ze = z - overhang * s
        left = [(-H - overhang, ze), (0.0, zr), (0.0, zr + T), (-H - overhang, ze + T)]
        right = [(H + overhang, ze), (0.0, zr), (0.0, zr + T), (H + overhang, ze + T)]
        out += [(prism(left, to3d, r0, r1, cap_tags=("end", "end")), "roof"),
                (prism(right, to3d, r0, r1, cap_tags=("end", "end")), "roof")]
        tri = [(-H, z), (H, z), (0.0, zr)]
        infill = [tri]
    elif style == "shed":
        sign = 1
        if up:
            upv = DIR_VECS.get(up)
            if upv is None or upv[aa] == 0:
                raise BuildError(f"{where}: shed 'up' must point across the ridge axis ({'+-'[0]}{'xy'[aa]} or -{'xy'[aa]})")
            sign = 1 if upv[aa] > 0 else -1
        w2 = 2 * H
        s = rise / w2
        T = thick * math.sqrt(1 + s * s)
        a_lo, a_hi = -sign * (H + overhang), sign * (H + overhang)
        poly = [(a_lo, z - overhang * s), (a_hi, z + rise + overhang * s),
                (a_hi, z + rise + overhang * s + T), (a_lo, z - overhang * s + T)]
        out.append((prism(poly, to3d, r0, r1, cap_tags=("end", "end")), "roof"))
        infill = [[(-H, z), (H, z), (sign * H, z + rise)]]
        if gables:
            gt = gables["thickness"]
            # the high long wall continues up to the roof
            hw_poly = [(sign * H, z), (sign * (H - gt), z), (sign * (H - gt), z + rise - gt * s), (sign * H, z + rise)]
            out.append((prism(hw_poly, to3d, lo[ra] + gt, hi[ra] - gt, cap_tags=("side", "side")), "gable"))
    elif style == "barrel":
        # a circular arc from eave to eave through the ridge; the ring's angles
        # are symmetric about straight up, so which way basis u points along
        # the across axis doesn't matter
        R = (H * H + rise * rise) / (2 * rise)
        cz = z + rise - R
        th0 = math.degrees(math.atan2(z - cz, H))
        arc0, arc1 = th0, 180 - th0
        n = int(segments or 16)
        out += [(p, "roof") for p in ring_pieces(to3d(0.0, cz, r0), to3d(0.0, cz, r1), R, R + thick,
                                                 arc0, arc1, n, where)]
        infill = [[(R * math.cos(t), cz + R * math.sin(t)) for t in ring_angles(arc0, arc1, n)]]
    else:
        raise BuildError(f"{where}: 'style' must be gable, shed, barrel or flat")
    if gables:
        gt = gables["thickness"]
        for poly in infill:
            for d0, d1 in ((lo[ra], lo[ra] + gt), (hi[ra] - gt, hi[ra])):
                pc = prism(poly, to3d, d0, d1, cap_tags=("side", "side"))
                if pc:
                    out.append((pc, "gable"))
    return [(p, role) for p, role in out if p]


def shape_roof(spec, where, default_gable_mat=None):
    mins, maxs = _vec(spec, "mins", 2, where), _vec(spec, "maxs", 2, where)
    x0, x1 = sorted((mins[0], maxs[0]))
    y0, y1 = sorted((mins[1], maxs[1]))
    z = _num(spec, "z", where=where)
    style = str(spec.get("style", "gable")).lower()
    up = spec.get("up")
    axis = spec.get("axis")
    if axis is None:
        if style == "shed" and up:
            axis = "y" if str(up).lower()[1] == "x" else "x"
        else:
            axis = "x" if (x1 - x0) >= (y1 - y0) else "y"
    axis = str(axis).lower()
    across = (y1 - y0) if axis == "x" else (x1 - x0)
    if "rise" in spec:
        rise = float(spec["rise"])
    elif "pitch" in spec:
        half = across if style == "shed" else across / 2
        rise = half * math.tan(math.radians(float(spec["pitch"])))
    else:
        rise = across / 4 if style != "flat" else 0
    over = float(spec.get("overhang", 16))
    end_over = float(spec.get("end_overhang", over))
    thick = float(spec.get("thickness", 8))
    g = spec.get("gables", True)
    gables = None
    if g:
        gables = {"thickness": float(g.get("thickness", 16)) if isinstance(g, dict) else 16.0,
                  "material": (g.get("material") if isinstance(g, dict) else None) or default_gable_mat}
    return roof_pieces(style, x0, y0, x1, y1, z, axis, rise, over, end_over, thick, gables,
                       spec.get("segments"), str(up).lower() if up else None, where), gables


def polyline_spans(pts, closed, gaps, where):
    """Split a polyline into solid runs (lists of 2D points) around gaps given
    as [start, end] distances along it. Returns (runs, total length, loop):
    loop is True for a closed polyline without gaps (mitered all round)."""
    P = [(float(p[0]), float(p[1])) for p in pts]
    V = P + [P[0]] if closed else P
    seg = [math.hypot(V[i + 1][0] - V[i][0], V[i + 1][1] - V[i][1]) for i in range(len(V) - 1)]
    if any(s < 1e-6 for s in seg):
        raise BuildError(f"{where}: two consecutive points are the same")
    total = sum(seg)
    cum = [0.0]
    for s in seg:
        cum.append(cum[-1] + s)
    if not gaps:
        return [P], total, closed

    def at(dist):
        dist = dist % total if closed else max(0.0, min(total, dist))
        for i in range(len(seg)):
            if dist <= cum[i + 1] + 1e-9:
                t = (dist - cum[i]) / seg[i]
                return (V[i][0] + (V[i + 1][0] - V[i][0]) * t, V[i][1] + (V[i + 1][1] - V[i][1]) * t)
        return V[-1]
    gl = []
    for i, g in enumerate(gaps):
        if not isinstance(g, (list, tuple)) or len(g) != 2:
            raise BuildError(f"{where}.gaps[{i}]: want [start, end] distances along the wall")
        a, b = sorted((float(g[0]), float(g[1])))
        gl.append((max(0.0, a), min(total, b)))
    gl.sort()
    solid, pos = [], 0.0
    for a, b in gl:
        if a > pos + 0.5:
            solid.append((pos, a))
        pos = max(pos, b)
    if total - pos > 0.5:
        solid.append((pos, total))
    if closed and len(solid) >= 2 and solid[0][0] < 1e-6 and solid[-1][1] > total - 1e-6:
        first, last = solid.pop(0), solid.pop()
        solid.append((last[0], total + first[1]))         # the run through the start point
    verts = [(cum[i], V[i]) for i in range(len(P))] if closed else [(cum[i], V[i]) for i in range(1, len(V) - 1)]
    if closed:
        verts += [(d + total, p) for d, p in verts]
    runs = []
    for a, b in solid:
        run = [at(a)]
        for d, p in sorted(verts):
            if a + 1e-6 < d < b - 1e-6:
                run.append(p)
        end = at(b)
        run.append(end)
        clean = [run[0]]
        for p in run[1:]:
            if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 0.01:
                clean.append(p)
        if len(clean) >= 2:
            runs.append(clean)
    return runs, total, False


def wall_run_pieces(run, z0, z1, t, align, loop, where):
    """Mitered vertical prisms along one polyline run."""
    off_l = {"center": t / 2, "left": t, "right": 0.0}[align]
    off_r = off_l - t
    n = len(run)
    if n < 2:
        return []
    segs = n if loop else n - 1

    def seg_dir(i):
        a, b = run[i % n], run[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        if ln < 1e-6:
            return None
        return (dx / ln, dy / ln)
    dirs = [seg_dir(i) for i in range(segs)]
    if any(dd is None for dd in dirs):
        raise BuildError(f"{where}: two consecutive points are the same")

    def offset_pt(i, off):
        """Mitered offset of vertex i (the left side is +off along the left normal)."""
        p = run[i % n]
        prev_d = dirs[(i - 1) % segs] if (loop or i > 0) else None
        next_d = dirs[i % segs] if (loop or i < segs) else None
        if prev_d is None or next_d is None:
            dd = next_d or prev_d
            return (p[0] - dd[1] * off, p[1] + dd[0] * off)
        n1 = (-prev_d[1], prev_d[0])
        n2 = (-next_d[1], next_d[0])
        bis = (n1[0] + n2[0], n1[1] + n2[1])
        bl = math.hypot(*bis)
        cosh = (n1[0] * n2[0] + n1[1] * n2[1] + 1) / 2
        if bl < 1e-6 or cosh < 0.07:                # near-reversal: no miter
            return (p[0] + n2[0] * off, p[1] + n2[1] * off)
        scale = off / math.sqrt(cosh) / bl
        return (p[0] + bis[0] * scale, p[1] + bis[1] * scale)
    L = [offset_pt(i, off_l) for i in range(n)]
    R = [offset_pt(i, off_r) for i in range(n)]
    pieces = []
    for i in range(segs):
        j = (i + 1) % n
        quad = [L[i], L[j], R[j], R[i]]
        if len(hull2d(quad)) < 4:
            # the miter folded over (a very short segment between sharp turns): square ends instead
            d = dirs[i]
            nl = (-d[1], d[0])
            a, b = run[i], run[j]
            quad = [(a[0] + nl[0] * off_l, a[1] + nl[1] * off_l), (b[0] + nl[0] * off_l, b[1] + nl[1] * off_l),
                    (b[0] + nl[0] * off_r, b[1] + nl[1] * off_r), (a[0] + nl[0] * off_r, a[1] + nl[1] * off_r)]
        pc = vertical_prism(quad, z0, z1)
        if pc:
            pieces.append(pc)
    return pieces


def posts_along(runs, loop, spacing, size, z0, z1):
    """Square posts at even intervals along each run (both ends included on
    open runs)."""
    posts = []
    if spacing <= 0:
        return posts
    for run in runs:
        seg = [(run[i], run[i + 1]) for i in range(len(run) - 1)] + ([(run[-1], run[0])] if loop else [])
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in seg]
        tot = sum(lens)
        k = max(1, int(round(tot / spacing)))
        for m in range(k + (0 if loop else 1)):
            d = tot * m / k
            for (a, b), ln in zip(seg, lens):
                if d <= ln + 1e-6:
                    tt = d / ln if ln > 1e-9 else 0
                    x, y = a[0] + (b[0] - a[0]) * tt, a[1] + (b[1] - a[1]) * tt
                    h = size / 2
                    posts.append(vertical_prism([(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)],
                                                z0, z1))
                    break
                d -= ln
    return posts


def _polyline(spec, where):
    pts = spec.get("points")
    if not isinstance(pts, list) or len(pts) < 2:
        raise BuildError(f"{where}: 'points' wants 2+ [x, y] points")
    return [tuple(vb._vec(p, 2, f"{where}.points[{i}]")) for i, p in enumerate(pts)]


def shape_wall(spec, where, fence=False):
    pts = _polyline(spec, where)
    z0, z1 = _zrange(spec, where)
    t = _num(spec, "thickness", 4 if fence else 16, where)
    align = str(spec.get("align", "center")).lower()
    if align not in ("center", "left", "right"):
        raise BuildError(f"{where}: 'align' must be center, left or right (of the walking direction)")
    closed = bool(spec.get("closed", False))
    runs, total, loop = polyline_spans(pts, closed, spec.get("gaps"), where)
    pieces = []
    for run in runs:
        pieces += wall_run_pieces(run, z0, z1, t, align, loop, where)
    posts = []
    if fence:
        posts = posts_along(runs, loop, float(spec.get("posts", 128)), float(spec.get("post_size", 6)), z0, z1 + 4)
    return pieces, posts, total


def shape_railing(spec, where):
    """Posts plus horizontal rails along a polyline standing on z."""
    pts = _polyline(spec, where)
    z = _num(spec, "z", where=where)
    h = _num(spec, "height", 40, where)
    rails = max(1, int(spec.get("rails", 2)))
    rs = _num(spec, "rail_size", 3, where)
    ps = _num(spec, "post_size", 4, where)
    closed = bool(spec.get("closed", False))
    runs, total, loop = polyline_spans(pts, closed, spec.get("gaps"), where)
    pieces = []
    for k in range(1, rails + 1):
        top = z + h * k / rails
        for run in runs:
            pieces += wall_run_pieces(run, top - rs, top, rs, "center", loop, where)
    posts = posts_along(runs, loop, float(spec.get("posts", 64)), ps, z, z + h)
    return pieces, posts, total


# ---------------------------------------------------------------------------
# Terrain (displacements)
# ---------------------------------------------------------------------------

def _hash01(ix, iy, seed):
    h = (ix * 374761393 + iy * 668265263 + seed * 2147483647) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFF) / float(0xFFFFFF)


def value_noise(x, y, seed):
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    sx, sy = smoothstep(fx), smoothstep(fy)
    a = _hash01(ix, iy, seed)
    b = _hash01(ix + 1, iy, seed)
    c = _hash01(ix, iy + 1, seed)
    d = _hash01(ix + 1, iy + 1, seed)
    return (a + (b - a) * sx) * (1 - sy) + (c + (d - c) * sx) * sy


def _dist_to_rect(x, y, r):
    dx = max(r[0] - x, 0.0, x - r[2])
    dy = max(r[1] - y, 0.0, y - r[3])
    return math.hypot(dx, dy)


def _dist_to_segment(x, y, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((x - a[0]) * vx + (y - a[1]) * vy) / l2))
    return math.hypot(x - a[0] - vx * t, y - a[1] - vy * t)


def _point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0 + 1e-300) + x0:
            inside = not inside
    return inside


def _dist_to_poly(x, y, poly):
    if _point_in_poly(x, y, poly):
        return 0.0
    return min(_dist_to_segment(x, y, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def height_function(spec, rect, where):
    """h(x, y) above the terrain's base z, from hills / ridges / noise / edge,
    multiplied by the 'flat' masks."""
    hills = []
    for i, hl in enumerate(spec.get("hills") or []):
        w = f"{where}.hills[{i}]"
        hills.append((vb._vec(hl.get("at"), 2, w + ".at"), _num(hl, "radius", where=w), _num(hl, "height", where=w),
                      str(hl.get("shape", "smooth")).lower()))
    ridges = []
    for i, rg in enumerate(spec.get("ridges") or []):
        w = f"{where}.ridges[{i}]"
        pts = [vb._vec(p, 2, w + ".points") for p in (rg.get("points") or [rg.get("from"), rg.get("to")])]
        ridges.append((pts, _num(rg, "width", where=w), _num(rg, "height", where=w)))
    nz = spec.get("noise")
    noise = None
    if nz:
        noise = (float(nz.get("amp", 32)), float(nz.get("scale", 512)), int(nz.get("seed", 1)),
                 int(nz.get("octaves", 3)))
    edge = spec.get("edge")
    edge_v = (float(edge.get("height", 256)), float(edge.get("width", 768))) if edge else None
    flats = []
    for i, f in enumerate(spec.get("flat") or []):
        w = f"{where}.flat[{i}]"
        blend = float(f.get("blend", 256))
        if "polygon" in f:
            poly = [tuple(vb._vec(p, 2, w + ".polygon")) for p in f["polygon"]]
            flats.append(("poly", poly, blend))
        else:
            mn, mx = vb._vec(f.get("mins"), 2, w + ".mins"), vb._vec(f.get("maxs"), 2, w + ".maxs")
            flats.append(("rect", (min(mn[0], mx[0]), min(mn[1], mx[1]), max(mn[0], mx[0]), max(mn[1], mx[1])), blend))
    clamp = spec.get("clamp")

    def h(x, y):
        v = 0.0
        for (hx, hy), r, ht, shape in hills:
            d = math.hypot(x - hx, y - hy)
            if d < r:
                t = 1 - d / r
                v += ht * (t if shape == "cone" else smoothstep(t) if shape == "smooth" else
                           math.exp(-4.5 * (d / r) ** 2) - math.exp(-4.5) * (d / r))
        for pts, width, ht in ridges:
            d = min(_dist_to_segment(x, y, pts[k], pts[k + 1]) for k in range(len(pts) - 1))
            if d < width / 2:
                v += ht * smoothstep(1 - d / (width / 2))
        if noise:
            amp, scale, seed, octs = noise
            a, s, tot = 1.0, scale, 0.0
            for o in range(octs):
                tot += a * (value_noise(x / s, y / s, seed + o * 101) - 0.5) * 2
                a *= 0.5
                s *= 0.5
            v += amp * tot
        if edge_v:
            eh, ew = edge_v
            d = min(x - rect[0], rect[2] - x, y - rect[1], rect[3] - y)
            if d < ew:
                v += eh * smoothstep(1 - d / ew)
        m = 1.0
        for kind, shape, blend in flats:
            d = _dist_to_rect(x, y, shape) if kind == "rect" else _dist_to_poly(x, y, shape)
            m = min(m, smoothstep(d / blend) if blend > 0 else (0.0 if d <= 0 else 1.0))
        v *= m
        if clamp:
            v = max(float(clamp[0]), min(float(clamp[1]), v))
        return v
    return h


def terrain_solids(spec, alloc, where="terrain", visgroup=None):
    """Displacement brushes covering the rectangle. Returns (solids, h(x,y) -> world z, info)."""
    mins, maxs = _vec(spec, "mins", 2, where), _vec(spec, "maxs", 2, where)
    x0, x1 = sorted((mins[0], maxs[0]))
    y0, y1 = sorted((mins[1], maxs[1]))
    z = float(spec.get("z", 0))
    tile = float(spec.get("tile", 512))
    power = int(spec.get("power", 3))
    if power not in (2, 3, 4):
        raise BuildError(f"{where}: 'power' must be 2, 3 or 4")
    nx, ny = int(round((x1 - x0) / tile)), int(round((y1 - y0) / tile))
    if nx < 1 or ny < 1 or abs(nx * tile - (x1 - x0)) > 0.01 or abs(ny * tile - (y1 - y0)) > 0.01:
        raise BuildError(f"{where}: the rectangle {vb.num(x1 - x0)} x {vb.num(y1 - y0)} isn't a whole number "
                         f"of {vb.num(tile)} tiles")
    thick = float(spec.get("thickness", 16))
    mat = spec.get("material") or "NATURE/BLENDGRASSGRASS001A"
    flat_mat = spec.get("flat_material") or mat
    side_mat = spec.get("side_material") or NODRAW
    lms = str(spec.get("lightmapscale", 32))
    skip_flat = spec.get("skip_flat", True)
    h = height_function(spec, (x0, y0, x1, y1), where)
    alpha = spec.get("alpha")
    n = (1 << power) + 1
    solids = []
    ndisp = nflat = 0
    hmin, hmax = 1e30, -1e30
    color = "160 200 90"
    for ty in range(ny):
        for tx in range(nx):
            bx0, by0 = x0 + tx * tile, y0 + ty * tile
            bx1, by1 = bx0 + tile, by0 + tile
            s = vmf.make_box((bx0, by0, z - thick), (bx1, by1, z), side_mat, alloc, lightmapscale=lms, color=color)
            vb._set_editor(s, vb.editor_block(color, visgroup))
            top = next(sd for sd in s.children("side") if vb.side_axis(sd) == "+z")
            top.set_kv("material", flat_mat)
            wind = dict((id(sd), w) for sd, w in vmf.solid_windings(s))
            w = wind[id(top)]
            start = (bx0, by0, z)
            si = min(range(4), key=lambda i: (w[i][0] - start[0]) ** 2 + (w[i][1] - start[1]) ** 2)
            p0, p1, p2, p3 = (w[(si + k) % 4] for k in range(4))
            grid = []
            for i in range(n):
                e0 = lerp(p0, p1, i / (n - 1))
                e1 = lerp(p3, p2, i / (n - 1))
                row = []
                for j in range(n):
                    p = lerp(e0, e1, j / (n - 1))
                    row.append((p[0], p[1], h(p[0], p[1])))
                grid.append(row)
            hs = [c[2] for row in grid for c in row]
            hmin, hmax = min(hmin, min(hs)), max(hmax, max(hs))
            if skip_flat and max(abs(v) for v in hs) < 0.5:
                solids.append(s)
                nflat += 1
                continue
            top.set_kv("material", mat)
            solids.append(s)
            top.items.append(_dispinfo(power, start, grid, alpha))
            ndisp += 1
    return solids, (lambda x, y: z + h(x, y)), {"tiles": nx * ny, "displacements": ndisp, "flat": nflat,
                                                  "hmin": hmin, "hmax": hmax}


def _vertex_normal(grid, i, j):
    n = len(grid)
    a = grid[max(0, i - 1)][j]
    b = grid[min(n - 1, i + 1)][j]
    c = grid[i][max(0, j - 1)]
    d = grid[i][min(n - 1, j + 1)]
    nv = cross(sub(b, a), sub(d, c))
    if nv[2] < 0:
        nv = mul(nv, -1)
    ln = length(nv)
    return (0.0, 0.0, 1.0) if ln < 1e-9 else mul(nv, 1 / ln)


def _dispinfo(power, start, grid, alpha):
    n = len(grid)
    normals, dists, offsets, onormals, alphas, tags = [], [], [], [], [], []
    a_lo, a_hi = 0.9, 0.7
    if isinstance(alpha, dict):
        a_lo, a_hi = float(alpha.get("from", 0.9)), float(alpha.get("to", 0.7))
    for i in range(n):
        nr, dr, al = [], [], []
        for j in range(n):
            hv = grid[i][j][2]
            nr.append("0 0 1" if hv >= 0 else "0 0 -1")
            dr.append(fmt_f(abs(hv)))
            if alpha:
                nz = _vertex_normal(grid, i, j)[2]
                t = (a_lo - nz) / (a_lo - a_hi) if a_lo != a_hi else 0
                al.append(str(int(round(255 * smoothstep(t)))))
            else:
                al.append("0")
        normals.append(" ".join(nr))
        dists.append(" ".join(dr))
        offsets.append(" ".join(["0 0 0"] * n))
        onormals.append(" ".join(["0 0 1"] * n))
        alphas.append(" ".join(al))
    for i in range(n - 1):
        row = []
        for j in range(n - 1):
            v00, v01, v10, v11 = grid[i][j], grid[i][j + 1], grid[i + 1][j], grid[i + 1][j + 1]
            for tri in ((v00, v10, v11), (v00, v11, v01)):
                nv = cross(sub(tri[1], tri[0]), sub(tri[2], tri[0]))
                ln = length(nv)
                nz = abs(nv[2]) / ln if ln > 1e-9 else 1.0
                row.append("9" if nz >= 0.8 else "1" if nz >= 0.7 else "0")
        tags.append(" ".join(row))

    def rows(name, lines):
        return Node(name, [KV("row%d" % k, ln) for k, ln in enumerate(lines)])
    return Node("dispinfo", [
        KV("power", str(power)),
        KV("startposition", "[%s]" % vb.fmt_pt(start)),
        KV("flags", "0"),
        KV("elevation", "0"),
        KV("subdiv", "0"),
        rows("normals", normals),
        rows("distances", dists),
        rows("offsets", offsets),
        rows("offset_normals", onormals),
        rows("alphas", alphas),
        rows("triangle_tags", tags),
        Node("allowed_verts", [KV("10", " ".join(["-1"] * 10))]),
    ])


# ---------------------------------------------------------------------------
# Spec -> pieces, transforms, into the map
# ---------------------------------------------------------------------------

def _rotation(spec):
    yaw, pitch, roll = (float(spec.get(k, 0)) for k in ("yaw", "pitch", "roll"))
    if not (yaw or pitch or roll):
        return None
    # Source angles: pitch about y (down-positive), yaw about z, roll about x
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp_ = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    # R = Rz(yaw) * Ry(pitch) * Rx(roll), with pitch positive = nose down
    m = [[cy * cp, cy * sp_ * sr - sy * cr, cy * sp_ * cr + sy * sr],
         [sy * cp, sy * sp_ * sr + cy * cr, sy * sp_ * cr - cy * sr],
         [-sp_, cp * sr, cp * cr]]
    return m


def _apply(m, pv, p):
    d = sub(p, pv)
    return (pv[0] + m[0][0] * d[0] + m[0][1] * d[1] + m[0][2] * d[2],
            pv[1] + m[1][0] * d[0] + m[1][1] * d[1] + m[1][2] * d[2],
            pv[2] + m[2][0] * d[0] + m[2][1] * d[1] + m[2][2] * d[2])


def _xform(spec):
    """A prefab placement's transform (set by the layout's 'place'): rotate by
    yaw about the prefab's local origin, then move to 'origin'. None if absent."""
    x = spec.get("_xform")
    if not x:
        return None
    return _rotation({"yaw": x.get("yaw", 0)}), tuple(float(c) for c in x.get("origin", (0, 0, 0)))


def transform_point(p, spec, where="shape"):
    """Where a point given in the spec's coordinates ends up after the shape's
    own rotation (about the pivot its pieces used) and any prefab placement."""
    m = _rotation(spec)
    if m is not None:
        pv = spec.get("_pivot_used")
        if pv is None:
            pv = tuple(vb._vec(spec["pivot"], 3, where + ".pivot")) if "pivot" in spec else p
        p = _apply(m, pv, p)
    xf = _xform(spec)
    if xf:
        mx, org = xf
        p = add(_apply(mx, (0.0, 0.0, 0.0), p) if mx else p, org)
    return p


def transform_pieces(pieces, spec, where):
    m = _rotation(spec)
    xf = _xform(spec)
    if m is None and xf is None:
        return pieces
    pv = None
    if m is not None:
        pts = [p for pc in pieces for f, _t in pc for p in f]
        if "pivot" in spec:
            pv = tuple(vb._vec(spec["pivot"], 3, where + ".pivot"))
        else:
            pv = tuple((min(p[i] for p in pts) + max(p[i] for p in pts)) / 2 for i in range(3))
        spec["_pivot_used"] = pv
    cache = {}

    def tr(p):
        if p not in cache:
            q = _apply(m, pv, p) if m is not None else p
            if xf:
                mx, org = xf
                q = add(_apply(mx, (0.0, 0.0, 0.0), q) if mx else q, org)
            cache[p] = q
        return cache[p]
    return [[([tr(p) for p in f], t) for f, t in pc] for pc in pieces]


def snapper_for(spec):
    g = spec.get("snap")
    if g:
        g = float(g)
        return lambda p: tuple(vb.num(round(c / g) * g) for c in p)
    return lambda p: tuple(vb.num(c) for c in p)


def make_pieces(spec, where="shape", default_gable_mat=None):
    """[(piece, role)] for a non-terrain shape spec; role picks the material
    set ('main', 'gable', 'post')."""
    t = str(spec.get("type", "")).lower()
    if t not in SHAPE_KEYS:
        raise BuildError(f"{where}: 'type' must be one of {', '.join(sorted(SHAPE_KEYS))}, got {t!r}")
    extra = {}
    if t == "box":
        pieces = [(p, "main") for p in shape_box(spec, where)]
    elif t == "prism":
        pieces = [(p, "main") for p in shape_prism(spec, where)]
    elif t == "wedge":
        pieces = [(p, "main") for p in shape_wedge(spec, where)]
        extra["slope"] = wedge_slope_deg(spec)
    elif t == "cylinder":
        pieces = [(p, "main") for p in shape_cylinder(spec, where)]
    elif t == "cone":
        pieces = [(p, "main") for p in shape_cone(spec, where)]
    elif t == "ring":
        pieces = [(p, "main") for p in shape_ring(spec, where)]
    elif t == "arch":
        pieces = [(p, "main") for p in shape_arch(spec, where)]
    elif t == "roof":
        rp, gables = shape_roof(spec, where, default_gable_mat)
        pieces = [(p, "main" if role == "roof" else "gable") for p, role in rp]
        extra["gable_material"] = gables["material"] if gables else None
    elif t in ("wall", "fence", "railing"):
        if t == "railing":
            wp, posts, total = shape_railing(spec, where)
        else:
            wp, posts, total = shape_wall(spec, where, fence=(t == "fence"))
        pieces = [(p, "main") for p in wp] + [(p, "post") for p in posts]
        extra["length"] = total
    else:
        raise BuildError(f"{where}: terrain is built with terrain_solids()")
    if not pieces:
        raise BuildError(f"{where}: produced no brushes")
    rotated = transform_pieces([p for p, _r in pieces], spec, where)
    return [(rp, role) for rp, (_p, role) in zip(rotated, pieces)], extra


FENCE_DEFAULTS = {"material": "METAL/METALFENCE001A", "post_material": "METAL/METALWALL012B",
                  "entity": "func_detail"}
RAILING_DEFAULTS = {"material": "METAL/METALWALL012B", "entity": "func_detail"}


def add_shape(geo, spec, d, where="shape", warn=print, terrains=None):
    """Build one shape spec into geo's document. Returns a short report dict."""
    t = str(spec.get("type", "")).lower()
    if t not in SHAPE_KEYS:
        raise BuildError(f"{where}: 'type' must be one of {', '.join(sorted(SHAPE_KEYS))}, got {t!r}")
    vb._check_keys(spec, COMMON_KEYS | SHAPE_KEYS[t], where, warn)
    if t == "terrain":
        solids, hfn, info = terrain_solids(spec, geo.alloc, where, geo.visgroup)
        for s in solids:
            geo.add_solid(s)
        if terrains is not None:
            mins, maxs = vb._vec(spec["mins"], 2), vb._vec(spec["maxs"], 2)
            terrains.append(((min(mins[0], maxs[0]), min(mins[1], maxs[1]), max(mins[0], maxs[0]),
                              max(mins[1], maxs[1])), hfn))
        return {"type": t, "brushes": len(solids), **info}
    fence = t == "fence"
    base = dict(FENCE_DEFAULTS) if fence else dict(RAILING_DEFAULTS) if t == "railing" else {}
    base.update({k: v for k, v in spec.items()})
    spec = base
    pieces, extra = make_pieces(spec, where, default_gable_mat=d.get("wall_material"))
    cls = spec.get("entity") or ("func_detail" if spec.get("detail") else None)
    if cls and cls.startswith("trigger_") and not spec.get("material"):
        spec["material"] = "TOOLS/TOOLSTRIGGER"
    mat = spec.get("material") or d.get("box_material") or "DEV/DEV_MEASUREGENERIC01"
    mats = dict(spec.get("materials") or {})
    if fence:
        mats.setdefault("top", NODRAW)
        mats.setdefault("bottom", NODRAW)
    snap = snapper_for(spec)
    lms = str(spec.get("lightmapscale", 16))
    tex_scale = float(spec.get("tex_scale", 0.25))
    color = spec.get("color", "0 180 137")
    solids, bad = [], 0
    post_solids = []
    for pc, role in pieces:
        if role == "gable":
            pm, pmats = extra.get("gable_material") or d.get("wall_material") or mat, {}
        elif role == "post":
            pm, pmats = spec.get("post_material") or mat, {}
        else:
            pm, pmats = mat, mats
        s = make_solid(pc, geo.alloc, pm, pmats, snap, color=color, visgroup=geo.visgroup, lightmapscale=lms,
                       tex_scale=tex_scale)
        if s is None:
            bad += 1
            continue
        (post_solids if role == "post" else solids).append(s)
    if bad:
        warn(f"WARN {where}: {bad} piece(s) collapsed when snapped and were dropped")
    solids += post_solids
    io = 0
    if cls:
        kvs = [("targetname", spec["name"])] if spec.get("name") else []
        if cls.startswith("trigger_"):
            kvs.append(("spawnflags", "1"))           # clients
        for k, v in (spec.get("kv") or {}).items():
            kvs = [kv for kv in kvs if kv[0] != k] + [(k, str(v))]
        origin = None
        if "origin" in spec:                          # rotation axis of doors / rotating brushes
            origin = transform_point(tuple(vb._vec(spec["origin"], 3, where + ".origin")), spec, where)
        ent = vb.add_entity(geo.doc, geo.alloc, cls, kvs, origin, visgroup=geo.visgroup)
        for s in solids:
            ent.insert_child_before_editor(s)
            geo._info[s] = vb.analyse(s)
        geo.added += len(solids)
        io = vb.add_outputs(ent, spec.get("outputs"), where)
    else:
        if spec.get("outputs"):
            raise BuildError(f"{where}: 'outputs' needs an 'entity' (world brushes have no I/O)")
        for s in solids:
            geo.add_solid(s)
    rep = {"type": t, "brushes": len(solids), "entity": cls, "io": io}
    rep.update({k: v for k, v in extra.items() if k in ("slope", "length")})
    return rep


# ---------------------------------------------------------------------------
# repeat / scatter helpers for the layout
# ---------------------------------------------------------------------------

def expand_repeat(spec, where):
    """A spec with "repeat": {"count": N or [nx, ny(, nz)], "step": [dx, dy(, dz)]}
    as a list of specs, each with its coordinates offset."""
    rp = spec.get("repeat")
    if not rp:
        return [spec]
    cnt = rp.get("count", 1)
    step = rp.get("step")
    if step is None:
        raise BuildError(f"{where}.repeat: needs 'step'")
    counts = [int(c) for c in (cnt if isinstance(cnt, list) else [cnt])]
    steps = step if isinstance(step[0], list) else None
    if steps is None:
        step = [float(c) for c in step] + [0.0] * (3 - len(step))
        if len(counts) == 1:
            steps = [step]
        else:
            # [nx, ny] with step [dx, dy]: x and y grid
            steps = [[step[0], 0, 0], [0, step[1], 0], [0, 0, step[2]]][:len(counts)]
    else:
        steps = [[float(c) for c in s] + [0.0] * (3 - len(s)) for s in steps]
    if len(steps) != len(counts):
        raise BuildError(f"{where}.repeat: 'count' and 'step' don't match")
    out = []

    def rec(level, off):
        if level == len(counts):
            out.append(offset_spec(spec, off))
            return
        for k in range(counts[level]):
            rec(level + 1, [off[i] + steps[level][i] * k for i in range(3)])
    rec(0, [0.0, 0.0, 0.0])
    return out


_POINT_KEYS3 = ("mins", "maxs", "center", "base", "from", "to", "origin", "pivot", "pos", "look_at", "start")
_POINT_LIST_KEYS = ("points", "polygon")


def offset_spec(spec, off):
    s = {k: v for k, v in spec.items() if k != "repeat"}
    if not any(off):
        return s
    for k in _POINT_KEYS3:
        if k in s and isinstance(s[k], (list, tuple)):
            v = [float(c) for c in s[k]]
            s[k] = [v[i] + off[i] for i in range(len(v))]
    for k in _POINT_LIST_KEYS:
        if k in s and isinstance(s[k], list):
            s[k] = [[float(p[0]) + off[0], float(p[1]) + off[1]] + [float(c) for c in p[2:]] for p in s[k]]
    if "z" in s and off[2]:
        z = s["z"]
        s["z"] = [float(z[0]) + off[2], float(z[1]) + off[2]] if isinstance(z, list) else float(z) + off[2]
    if "openings" in s and (off[0] or off[1]):
        pass  # openings are measured along the wall: unchanged
    return s


# ---------------------------------------------------------------------------
# CLI: shape
# ---------------------------------------------------------------------------

def _cmd_shape(args):
    specs = []
    for text in args.spec:
        if text.startswith("@"):
            with open(text[1:], "r", encoding="utf-8") as f:
                v = json.load(f)
        else:
            try:
                v = json.loads(text)
            except json.JSONDecodeError as e:
                print(f"error: spec is not valid JSON: {e}", file=sys.stderr)
                sys.exit(2)
        specs += v if isinstance(v, list) else [v]

    def fn(geo):
        reps = []
        for i, sp in enumerate(specs):
            for k, one in enumerate(expand_repeat(sp, f"spec[{i}]")):
                reps.append(add_shape(geo, one, vb.DEFAULTS, f"spec[{i}]"))
        lines = []
        for r in reps:
            extra = ""
            if "slope" in r:
                extra = f", slope {r['slope']:.1f} deg" + (" (too steep to walk)" if r["slope"] > 45.5 else "")
            if "displacements" in r:
                extra = f", {r['displacements']} displacement(s), height {vb.num(r['hmin'])}..{vb.num(r['hmax'])}"
            lines.append(f"{r['type']}: {r['brushes']} brush(es)" + (f" in {r['entity']}" if r.get("entity") else "") + extra)
        for ln in lines:
            print("  " + ln)
        return f"{len(reps)} shape(s), {geo.added} brush(es) added"
    vb._edit(args, fn)


def add_parsers(sub):
    sp = sub.add_parser("shape", help="add non-box brushes: box (rotated), prism, wedge, cylinder, cone, ring, "
                                      "arch, roof, wall, fence, terrain (JSON spec)")
    sp.add_argument("file")
    sp.add_argument("spec", nargs="+", help="JSON object (or list), or @file.json; format: SKILL.md 'Shapes'")
    sp.add_argument("--dry-run", action="store_true", help="show a diff, write nothing")
    sp.set_defaults(func=_cmd_shape)
