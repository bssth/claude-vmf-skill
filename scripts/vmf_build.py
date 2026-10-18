#!/usr/bin/env python3
"""vmf_build.py - greybox / blockout generation on top of vmf.make_box.

  room      hollow box (floor, ceiling, 4 walls) around an interior, with doors
            and windows cut into the walls
  corridor  hollow axis-aligned passage between points; it opens into the
            rooms / walls it runs into and is capped where it ends in the open
  stairs    solid steps from a start point, plus a player-clip ramp over them
  carve     cut an axis-aligned hole through existing axis-aligned world brushes
  build     generate a whole blockout from one JSON layout file
  new-map   write an empty map to build into

Rooms and corridors are given by their INTERIOR (the air inside); the floor,
ceiling and walls are built around it, so a room's floor top is its interior
min z. Everything is axis-aligned boxes (the stair clip ramp is the one
sloped brush). Ids come from vmf.IdAllocator; every brush made here is checked
the way `lint invalid_brushes` checks it before anything is written.

Holes are cut by splitting boxes: the box minus the hole becomes up to 6 boxes,
split along x, then y, then z, so wall pieces beside a door keep the full wall
height and the lintel / sill pieces sit between them. Each piece keeps the
material and texture axes the original box had on the face pointing the same
way. Brushes that aren't axis-aligned boxes (sloped, displacements) are left
alone and reported.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

import vmf
from vmf import KV, Node

EPS = 0.01

DEFAULTS = {
    "wall": 16,
    "floor": 16,
    "ceiling": 16,
    "wall_material": "DEV/DEV_MEASUREWALL01A",
    "floor_material": "DEV/DEV_MEASUREGENERIC01B",
    "ceiling_material": "DEV/DEV_MEASUREGENERIC01",
    "stairs_material": "DEV/DEV_MEASUREGENERIC01B",
    "box_material": "DEV/DEV_MEASUREGENERIC01",
    "roof_material": "DEV/DEV_MEASUREGENERIC01B",
    "clip_material": "TOOLS/TOOLSPLAYERCLIP",
    "light": 300,                 # brightness of automatic room lights; 0 = none
    "corridor_light": 120,        # brightness of automatic corridor lights; 0 = none
    "light_color": [255, 240, 220],
    "light_spacing": 320,         # one automatic light per cell of this size
}

OPENING_TYPES = {
    # type: (width, height, sill)
    "door": (64, 112, 0),
    "window": (96, 64, 48),
    "hole": (None, None, 0),
}

WALLS = {  # name: (normal axis, sign)
    "south": (1, -1), "-y": (1, -1),
    "north": (1, 1), "+y": (1, 1),
    "west": (0, -1), "-x": (0, -1),
    "east": (0, 1), "+x": (0, 1),
}

DIRS = {"+x": (0, 1), "-x": (0, -1), "+y": (1, 1), "-y": (1, -1)}

# Brushes made only of these don't stop a corridor / opening from "reaching" a
# wall (clips, hints, triggers...). They are still cut by carve.
_NONBLOCKING = {
    "TOOLS/TOOLSCLIP", "TOOLS/TOOLSPLAYERCLIP", "TOOLS/TOOLSNPCCLIP", "TOOLS/TOOLSHINT",
    "TOOLS/TOOLSSKIP", "TOOLS/TOOLSTRIGGER", "TOOLS/TOOLSINVISIBLE", "TOOLS/TOOLSBLOCK_LOS",
    "TOOLS/TOOLSBLOCKBULLETS", "TOOLS/TOOLSBLOCKLIGHT", "TOOLS/TOOLSOCCLUDER",
    "TOOLS/TOOLSAREAPORTAL", "TOOLS/TOOLSINVISIBLELADDER",
}


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------
# Boxes: (x0, y0, z0, x1, y1, z1) tuples
# ---------------------------------------------------------------------------

def num(v):
    """Round to 1/1000 and drop a trailing .0 (planes print like Hammer's)."""
    r = round(float(v), 3)
    return int(r) if r == int(r) else r


def mkbox(mins, maxs):
    return (tuple(num(min(mins[i], maxs[i])) for i in range(3))
            + tuple(num(max(mins[i], maxs[i])) for i in range(3)))


def overlaps(a, b, eps=EPS):
    """True when the boxes share volume (touching faces don't count)."""
    return all(a[i] < b[i + 3] - eps and b[i] < a[i + 3] - eps for i in range(3))


def point_in(p, b, eps=EPS):
    return all(b[i] - eps <= p[i] <= b[i + 3] + eps for i in range(3))


def subtract(b, h, order=(0, 1, 2)):
    """Box `b` minus box `h` as up to 6 boxes, split along `order`'s axes in
    turn (the first axis' pieces are the biggest)."""
    if not overlaps(b, h):
        return [b]
    out, cur = [], list(b)
    for ax in order:
        if h[ax] > cur[ax] + EPS:
            p = cur[:]
            p[ax + 3] = h[ax]
            out.append(tuple(p))
            cur[ax] = h[ax]
        if h[ax + 3] < cur[ax + 3] - EPS:
            p = cur[:]
            p[ax] = h[ax + 3]
            out.append(tuple(p))
            cur[ax + 3] = h[ax + 3]
    return out


def fmt_pt(p):
    return " ".join(vmf._fmt_coord(num(v)) for v in p)


def fmt_box(b):
    return "(%s)-(%s)" % (fmt_pt(b[:3]), fmt_pt(b[3:]))


# ---------------------------------------------------------------------------
# Brush construction
# ---------------------------------------------------------------------------

def clone(node):
    return Node(node.name, [KV(it.key, it.value) if isinstance(it, KV) else clone(it) for it in node.items])


def side_axis(side):
    """'+x' / '-z' ... for a side whose outward normal is axis-aligned, else None."""
    try:
        pl = vmf.plane_from_points(*vmf.parse_plane_points(side.get("plane") or ""))
    except ValueError:
        return None
    if pl is None:
        return None
    n = pl[0]
    for i in range(3):
        if abs(abs(n[i]) - 1.0) < 1e-6:
            return ("+" if n[i] > 0 else "-") + "xyz"[i]
    return None


def editor_block(color, visgroup=None):
    items = [KV("color", color)]
    if visgroup:
        items.append(KV("visgroupid", visgroup))
    items += [KV("visgroupshown", "1"), KV("visgroupautoshown", "1")]
    return Node("editor", items)


def _set_editor(node, ed):
    for i, it in enumerate(node.items):
        if isinstance(it, Node) and it.name == "editor":
            node.items[i] = ed
            return
    node.items.append(ed)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def make_convex(faces, material, alloc, color="0 180 137", visgroup=None, lightmapscale="16"):
    """A solid from the face polygons of a convex polyhedron (each a list of
    3D points, either winding). Planes are oriented so vbsp's outward normal
    (p0-p1) x (p2-p1) points away from the centroid; texture axes are world
    aligned on the face's dominant axis, like make_box."""
    pts = [p for f in faces for p in f]
    cen = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    solid = Node("solid", [KV("id", alloc.take())])
    for poly in faces:
        p0, p1, p2 = poly[0], poly[1], poly[2]
        n = _cross(_sub(p1, p0), _sub(p2, p0))  # must point INTO the brush
        if _dot(n, _sub(cen, p0)) < 0:
            p1, p2 = p2, p1
            n = (-n[0], -n[1], -n[2])
        dom = max(range(3), key=lambda i: abs(n[i]))
        uvec, vvec = {2: ((1, 0, 0), (0, -1, 0)), 1: ((1, 0, 0), (0, 0, -1)), 0: ((0, 1, 0), (0, 0, -1))}[dom]
        side = Node("side", [KV("id", alloc.take()),
                             KV("plane", "(%s) (%s) (%s)" % (fmt_pt(p0), fmt_pt(p1), fmt_pt(p2)))])
        side.items.append(Node("vertices_plus", [KV("v", fmt_pt(p)) for p in poly]))
        side.add_kv("material", material)
        side.add_kv("uaxis", "[%d %d %d 0] 0.25" % uvec)
        side.add_kv("vaxis", "[%d %d %d 0] 0.25" % vvec)
        side.add_kv("rotation", "0")
        side.add_kv("lightmapscale", lightmapscale)
        side.add_kv("smoothing_groups", "0")
        solid.items.append(side)
    solid.items.append(editor_block(color, visgroup))
    return solid


def solid_ok(solid):
    """What lint's invalid_brushes check wants: the planes bound a volume."""
    return len(vmf.solid_windings(solid)) >= 4


def new_document(skyname="sky_day01_01"):
    def blk(name, *kvs):
        return Node(name, [KV(k, v) for k, v in kvs])
    return vmf.Document([
        blk("versioninfo", ("editorversion", "400"), ("editorbuild", "8000"), ("mapversion", "1"),
            ("formatversion", "100"), ("prefab", "0")),
        blk("visgroups"),
        blk("viewsettings", ("bSnapToGrid", "1"), ("bShowGrid", "1"), ("bShowLogicalGrid", "0"),
            ("nGridSpacing", "16"), ("bShow3DGrid", "0")),
        blk("world", ("id", "1"), ("mapversion", "1"), ("classname", "worldspawn"),
            ("detailmaterial", "detail/detailsprites"), ("detailvbsp", "detail.vbsp"),
            ("maxpropscreenwidth", "-1"), ("skyname", skyname)),
        blk("cameras", ("activecamera", "-1")),
        blk("cordons", ("active", "0")),
    ])


def ensure_visgroup(doc, name, color="255 160 0"):
    """Id of the top-level visgroup `name`, created if missing."""
    vg = doc.top("visgroups")
    if vg is None:
        vg = Node("visgroups", [])
        doc.roots.insert(1 if doc.roots and doc.roots[0].name == "versioninfo" else 0, vg)
    top = 0
    for n in vmf.iter_all(vg):
        if n.name == "visgroup":
            v = n.get("visgroupid") or ""
            if v.isdigit():
                top = max(top, int(v))
            if n.get("name") == name and n in list(vg.children("visgroup")):
                return v
    vid = str(top + 1)
    vg.items.append(Node("visgroup", [KV("name", name), KV("visgroupid", vid), KV("color", color)]))
    return vid


# ---------------------------------------------------------------------------
# The world as boxes: carving, probing, adding
# ---------------------------------------------------------------------------

class _Info:
    __slots__ = ("bbox", "sides", "why", "blocking")

    def __init__(self, bbox, sides, why, blocking=True):
        self.bbox, self.sides, self.why, self.blocking = bbox, sides, why, blocking


def analyse(solid):
    """bbox of a brush (from its planes) and, when it is an axis-aligned box,
    its sides by outward axis. `why` says why it can't be split."""
    faces = vmf.solid_windings(solid)
    if len(faces) < 4:
        return _Info(None, None, "invalid brush (planes bound no volume)", False)
    pts = [p for _s, w in faces for p in w]
    bbox = tuple(num(min(p[i] for p in pts)) for i in range(3)) + tuple(num(max(p[i] for p in pts)) for i in range(3))
    mats = {(s.get("material") or "").upper() for s, _w in faces}
    blocking = not mats <= _NONBLOCKING
    sides, why = {}, None
    for side, _w in faces:
        if side.first("dispinfo") is not None:
            why = "displacement"
            break
        ax = side_axis(side)
        if ax is None:
            why = "not axis-aligned"
            break
        sides.setdefault(ax, side)
    if why is None and len(sides) != 6:
        why = "not a box"
    return _Info(bbox, None if why else sides, why, blocking)


def _quick_bbox(solid):
    """bbox from Hammer++'s vertices_plus (a fast pre-filter), None if a side lacks them."""
    lo, hi = [1e30] * 3, [-1e30] * 3
    for side in solid.children("side"):
        vp = side.first("vertices_plus")
        if vp is None:
            return None
        for v in vp.get_all("v"):
            try:
                p = [float(c) for c in v.split()]
            except ValueError:
                return None
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])
    if lo[0] > hi[0]:
        return None
    return tuple(v - 1 for v in lo) + tuple(v + 1 for v in hi)


class Geo:
    """A document's world brushes seen as boxes. All changes go through here so
    ids come from one allocator and every added brush is cached."""

    def __init__(self, doc, alloc, visgroup=None, detail=False):
        self.doc = doc
        self.alloc = alloc
        self.visgroup = visgroup
        self.detail = detail          # also carve func_detail brushes
        self.world = doc.top("world")
        if self.world is None:
            raise BuildError("the map has no world block")
        self._info = {}
        self._quick = {}
        self.added = 0
        self.cut = 0
        self.pieces = 0
        self.skipped = {}             # solid id -> why it wasn't cut
        self.next_node = 1            # nodeid for generated info_nodes

    # -- queries ------------------------------------------------------------

    def containers(self):
        yield self.world
        if self.detail:
            for e in vmf.iter_entities(self.doc):
                if (e.get("classname") or "").lower() == "func_detail":
                    yield e

    def solids(self):
        for c in self.containers():
            for s in c.children("solid"):
                yield s

    def info(self, solid):
        inf = self._info.get(solid)
        if inf is None:
            inf = self._info[solid] = analyse(solid)
        return inf

    def _may_overlap(self, solid, box):
        if solid in self._info:
            bb = self._info[solid].bbox
            return bb is not None and overlaps(bb, box)
        if solid not in self._quick:
            self._quick[solid] = _quick_bbox(solid)
        q = self._quick[solid]
        return q is None or overlaps(q, box)

    def overlapping(self, box, blocking_only=True):
        out = []
        for s in self.solids():
            if self._may_overlap(s, box):
                inf = self.info(s)
                if inf.bbox is not None and overlaps(inf.bbox, box) and (inf.blocking or not blocking_only):
                    out.append(s)
        return out

    # -- changes ------------------------------------------------------------

    def add_box(self, box, material, color="0 180 137"):
        s = vmf.make_box(box[:3], box[3:], material, self.alloc, color=color)
        _set_editor(s, editor_block(color, self.visgroup))
        self.world.items.append(s)
        self._info[s] = analyse(s)
        self.added += 1
        return s

    def add_solid(self, solid):
        self.world.items.append(solid)
        self._info[solid] = analyse(solid)
        self.added += 1
        return solid

    def _piece(self, box, inf, original):
        s = vmf.make_box(box[:3], box[3:], "TOOLS/TOOLSNODRAW", self.alloc)
        for side in s.children("side"):
            t = inf.sides.get(side_axis(side))
            if t is None:
                continue
            for key in ("material", "uaxis", "vaxis", "rotation", "lightmapscale", "smoothing_groups"):
                v = t.get(key)
                if v is not None:
                    side.set_kv(key, v)
        ed = original.first("editor")
        if ed is not None:
            _set_editor(s, clone(ed))
        self._info[s] = analyse(s)
        return s

    def carve(self, hole):
        """Remove box `hole` from every axis-aligned box brush it overlaps,
        replacing each with the boxes around the hole. Returns the number of
        brushes cut; brushes that can't be split go to self.skipped."""
        cut = 0
        empty_entities = []
        for cont in list(self.containers()):
            new_items, changed = [], False
            for it in cont.items:
                if not (isinstance(it, Node) and it.name == "solid") or not self._may_overlap(it, hole):
                    new_items.append(it)
                    continue
                inf = self.info(it)
                if inf.bbox is None or not overlaps(inf.bbox, hole):
                    new_items.append(it)
                    continue
                if inf.sides is None:
                    self.skipped[it.get("id")] = inf.why
                    new_items.append(it)
                    continue
                parts = subtract(inf.bbox, hole)
                new_items.extend(self._piece(p, inf, it) for p in parts)
                cut += 1
                self.pieces += len(parts)
                changed = True
            if changed:
                cont.items = new_items
                if cont is not self.world and cont.first("solid") is None:
                    empty_entities.append(cont)
        if empty_entities:
            self.doc.roots = [r for r in self.doc.roots if all(r is not e for e in empty_entities)]
        self.cut += cut
        return cut

    def punch(self, hole, axis, sign, limit=512):
        """Grow `hole` along axis*sign through the brushes right past its far
        face until the far side is open (e.g. the second of two walls built back
        to back). Returns the grown box."""
        h = list(hole)
        start = h[axis + 3] if sign > 0 else h[axis]
        for _ in range(32):
            far = h[axis + 3] if sign > 0 else h[axis]
            probe = h[:]
            probe[axis], probe[axis + 3] = (far, far + 1) if sign > 0 else (far - 1, far)
            hits = self.overlapping(tuple(probe))
            if not hits:
                break
            if sign > 0:
                h[axis + 3] = min(max(self.info(s).bbox[axis + 3] for s in hits), start + limit)
            else:
                h[axis] = max(min(self.info(s).bbox[axis] for s in hits), start - limit)
            if abs((h[axis + 3] if sign > 0 else h[axis]) - start) >= limit:
                break
        return tuple(h)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def add_entity(doc, alloc, cls, kvs=(), origin=None, color="220 30 220", visgroup=None):
    ent = Node("entity", [KV("id", alloc.take()), KV("classname", cls)])
    for k, v in kvs:
        ent.add_kv(k, str(v))
    if origin is not None:
        ent.add_kv("origin", fmt_pt(origin))
    ent.items.append(editor_block(color, visgroup))
    idx = len(doc.roots)
    for i, r in enumerate(doc.roots):
        if r.name in ("cameras", "cordon", "cordons"):
            idx = i
            break
    doc.roots.insert(idx, ent)
    return ent


def add_outputs(ent, outputs, where="entity"):
    """Append I/O connections to an entity. Each output is a list
    [output, target, input, param, delay, times] (the last three optional) or a
    dict with those keys. Fields are joined with ',' like Hammer++ writes them,
    or with ESC when a field itself contains a comma."""
    if not outputs:
        return 0
    if not isinstance(outputs, list):
        raise BuildError(f"{where}: 'outputs' wants a list")
    conns = ent.first("connections")
    n = 0
    for i, o in enumerate(outputs):
        if isinstance(o, dict):
            f = [o.get("output"), o.get("target"), o.get("input"), o.get("param", ""), o.get("delay", 0),
                 o.get("times", -1)]
        elif isinstance(o, (list, tuple)) and 3 <= len(o) <= 6:
            f = list(o) + ["", 0, -1][len(o) - 3:]
        else:
            raise BuildError(f"{where}.outputs[{i}]: want [output, target, input, param, delay, times]")
        if not f[0] or not f[1] or not f[2]:
            raise BuildError(f"{where}.outputs[{i}]: output, target and input are required")
        vals = [str(f[1]), str(f[2]), str(f[3]), str(num(f[4])), str(int(f[5]))]
        sep = "\x1b" if any("," in v for v in vals) else ","
        if conns is None:
            conns = Node("connections", [])
            ent.insert_child_before_editor(conns)
        conns.add_kv(str(f[0]), sep.join(vals))
        n += 1
    return n


def light_kvs(brightness, color):
    c = list(color) + [255] * (3 - len(color))
    return [("_light", "%d %d %d %d" % (c[0], c[1], c[2], brightness)), ("_lightHDR", "-1 -1 -1 1"),
            ("_lightscaleHDR", "1"), ("_constant_attn", "0"), ("_linear_attn", "0"),
            ("_quadratic_attn", "1"), ("_fifty_percent_distance", "0"), ("_zero_percent_distance", "0"),
            ("_hardfalloff", "0"), ("spawnflags", "0"), ("style", "0")]


def look_angles(pos, target):
    dx, dy, dz = (target[i] - pos[i] for i in range(3))
    yaw = math.degrees(math.atan2(dy, dx)) % 360
    pitch = -math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return (num(pitch), num(yaw), 0)


def fmt_angles(a):
    return " ".join(vmf._fmt_coord(num(v)) for v in a)


# ---------------------------------------------------------------------------
# Spec helpers
# ---------------------------------------------------------------------------

def _vec(v, n=3, what="value"):
    if isinstance(v, str):
        v = v.replace(",", " ").split()
    try:
        out = [float(c) for c in v]
    except (TypeError, ValueError):
        raise BuildError(f"{what}: want {n} numbers, got {v!r}")
    if len(out) != n:
        raise BuildError(f"{what}: want {n} numbers, got {v!r}")
    return out


def _check_keys(spec, allowed, where, warn):
    for k in spec:
        if k not in allowed and not k.startswith("_") and k != "comment":
            warn(f"{where}: unknown key '{k}' (ignored)")


def _interior(spec, where):
    if "mins" not in spec:
        raise BuildError(f"{where}: needs 'mins' and 'maxs' (or 'size')")
    mins = _vec(spec["mins"], 3, where + ".mins")
    if "maxs" in spec:
        maxs = _vec(spec["maxs"], 3, where + ".maxs")
    elif "size" in spec:
        size = _vec(spec["size"], 3, where + ".size")
        maxs = [mins[i] + size[i] for i in range(3)]
    else:
        raise BuildError(f"{where}: needs 'maxs' or 'size'")
    b = mkbox(mins, maxs)
    if any(b[i + 3] - b[i] < 1 for i in range(3)):
        raise BuildError(f"{where}: box {fmt_box(b)} is flat")
    return b


def _mat(spec, d, which):
    return spec.get(which + "_material") or spec.get("material") or d.get(which + "_material") or d["box_material"]


def _thick(spec, d, key):
    v = spec.get(key, d[key])
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise BuildError(f"'{key}' must be a number, got {v!r}")
    if v < 0:
        raise BuildError(f"'{key}' must be >= 0")
    return num(v)


def auto_lights(geo, box, brightness, color, spacing, z):
    """One light per spacing x spacing cell of `box`'s footprint, at height z."""
    if not brightness:
        return 0
    nx = max(1, int(math.ceil((box[3] - box[0]) / spacing - 0.25)))
    ny = max(1, int(math.ceil((box[4] - box[1]) / spacing - 0.25)))
    for i in range(nx):
        for j in range(ny):
            x = box[0] + (box[3] - box[0]) * (i + 0.5) / nx
            y = box[1] + (box[4] - box[1]) * (j + 0.5) / ny
            add_entity(geo.doc, geo.alloc, "light", light_kvs(brightness, color), (num(x), num(y), num(z)),
                       color="220 220 40", visgroup=geo.visgroup)
    return nx * ny


# ---------------------------------------------------------------------------
# room
# ---------------------------------------------------------------------------

ROOM_KEYS = {"name", "mins", "maxs", "size", "wall", "floor", "ceiling", "material", "wall_material",
             "floor_material", "ceiling_material", "openings", "light", "light_color", "nodes", "roof"}
OPENING_KEYS = {"wall", "type", "offset", "at", "width", "height", "sill", "repeat"}


def expand_openings(ops, where):
    """Openings with "repeat": {"count": N, "step": S} become N openings, each
    S further along the wall ('at' or 'offset' must be a number)."""
    out = []
    for i, op in enumerate(ops or []):
        if not isinstance(op, dict):
            raise BuildError(f"{where}.openings[{i}]: want an object")
        rp = op.get("repeat")
        if not rp:
            out.append((i, op))
            continue
        n, step = int(rp.get("count", 1)), float(rp.get("step", 0))
        key = "at" if "at" in op else "offset"
        if not isinstance(op.get(key), (int, float)):
            raise BuildError(f"{where}.openings[{i}]: 'repeat' needs a numeric 'at' or 'offset'")
        for k in range(n):
            o = {kk: vv for kk, vv in op.items() if kk != "repeat"}
            o[key] = float(op[key]) + k * step
            out.append((i, o))
    return out


def opening_hole(interior, wall_t, op, where):
    """(hole box, normal axis, outward sign, description) for one opening."""
    wall = str(op.get("wall", "")).lower()
    if wall not in WALLS:
        raise BuildError(f"{where}: 'wall' must be one of north/south/east/west (+y/-y/+x/-x), got {wall!r}")
    nax, sign = WALLS[wall]
    along = 1 - nax
    typ = str(op.get("type", "door")).lower()
    if typ not in OPENING_TYPES:
        raise BuildError(f"{where}: 'type' must be door, window or hole, got {typ!r}")
    dw, dh, ds = OPENING_TYPES[typ]
    width, height, sill = op.get("width", dw), op.get("height", dh), op.get("sill", ds)
    if width is None or height is None:
        raise BuildError(f"{where}: a 'hole' needs 'width' and 'height'")
    width, height, sill = float(width), float(height), float(sill)
    a0, a1 = interior[along], interior[along + 3]
    z0, z1 = interior[2], interior[5]
    if "at" in op:
        u0 = float(op["at"]) - width / 2
    elif op.get("offset") in (None, "center", "c"):
        u0 = (a0 + a1 - width) / 2
    else:
        u0 = a0 + float(op["offset"])
    if u0 < a0 - EPS or u0 + width > a1 + EPS:
        raise BuildError(f"{where}: {typ} {num(u0)}..{num(u0 + width)} along {'xy'[along]} doesn't fit the "
                         f"{wall} wall's inside span {num(a0)}..{num(a1)}")
    if sill < 0 or z0 + sill + height > z1 + EPS:
        raise BuildError(f"{where}: {typ} sill {num(sill)} + height {num(height)} doesn't fit the room's "
                         f"inside height {num(z1 - z0)}")
    lo, hi = [0.0] * 3, [0.0] * 3
    lo[along], hi[along] = u0, u0 + width
    lo[2], hi[2] = z0 + sill, z0 + sill + height
    if sign > 0:
        lo[nax], hi[nax] = interior[nax + 3], interior[nax + 3] + wall_t
    else:
        lo[nax], hi[nax] = interior[nax] - wall_t, interior[nax]
    desc = f"{typ} in the {wall} wall at {'xy'[along]} {num(u0)}..{num(u0 + width)}, z {num(lo[2])}..{num(hi[2])}"
    return mkbox(lo, hi), nax, sign, desc


def add_room(geo, spec, d, where="room", warn=print, openings=True):
    """Floor, ceiling and 4 walls around the interior box, then (unless
    openings=False) each opening carved through the wall and through anything
    built right behind it."""
    _check_keys(spec, ROOM_KEYS, where, warn)
    interior = _interior(spec, where)
    w, f, c = _thick(spec, d, "wall"), _thick(spec, d, "floor"), _thick(spec, d, "ceiling")
    x0, y0, z0, x1, y1, z1 = interior
    parts = []
    if f:
        parts.append(((x0 - w, y0 - w, z0 - f, x1 + w, y1 + w, z0), _mat(spec, d, "floor")))
    if c:
        parts.append(((x0 - w, y0 - w, z1, x1 + w, y1 + w, z1 + c), _mat(spec, d, "ceiling")))
    if w:
        wm = _mat(spec, d, "wall")
        parts += [((x0 - w, y0 - w, z0, x1 + w, y0, z1), wm),   # south (-y), owns the corners
                  ((x0 - w, y1, z0, x1 + w, y1 + w, z1), wm),   # north (+y)
                  ((x0 - w, y0, z0, x0, y1, z1), wm),           # west (-x)
                  ((x1, y0, z0, x1 + w, y1, z1), wm)]           # east (+x)
    for box, mat in parts:
        geo.add_box(mkbox(box[:3], box[3:]), mat)
    info = {"interior": interior, "openings": [], "brushes": len(parts)}
    roof = spec.get("roof")
    if roof:
        info["brushes"] += add_room_roof(geo, spec, d, roof, interior, w, c, where, warn)
    if openings:
        info["openings"] = add_room_openings(geo, spec, d, info, where, warn)
    return info


def add_room_roof(geo, spec, d, roof, interior, w, c, where, warn):
    """A roof (vmf_shapes 'roof' spec minus the footprint) over the room's outer
    walls, sitting on the walls (or on the ceiling slab when there is one). The
    gable-end infill gets the walls' thickness and material."""
    import vmf_shapes
    if roof is True:
        roof = {}
    if not isinstance(roof, dict):
        raise BuildError(f"{where}: 'roof' wants an object (vmf_shapes roof keys) or true")
    x0, y0, z0, x1, y1, z1 = interior
    rs = {"type": "roof", "style": "gable", "material": d.get("roof_material", DEFAULTS["roof_material"])}
    rs.update(roof)
    rs["mins"], rs["maxs"] = [x0 - w, y0 - w], [x1 + w, y1 + w]
    rs.setdefault("z", z1 + c)
    g = rs.get("gables", True)
    if g:
        g = dict(g) if isinstance(g, dict) else {}
        g.setdefault("thickness", w or 16)
        g.setdefault("material", _mat(spec, d, "wall"))
        rs["gables"] = g
    rep = vmf_shapes.add_shape(geo, rs, d, where + ".roof", warn)
    return rep["brushes"]


def add_room_openings(geo, spec, d, info, where="room", warn=print):
    """Cut each of the room's openings through its wall, and on through brushes
    built right behind it (a neighbour's wall)."""
    interior = info["interior"]
    w = _thick(spec, d, "wall")
    out = []
    for i, op in expand_openings(spec.get("openings"), where):
        ow = f"{where}.openings[{i}]"
        _check_keys(op, OPENING_KEYS, ow, warn)
        if not w:
            raise BuildError(f"{ow}: the room has no walls (wall 0)")
        hole, nax, sign, desc = opening_hole(interior, w, op, ow)
        geo.carve(hole)
        through = geo.punch(hole, nax, sign)
        if through != hole:
            geo.carve(through)
        out.append({"hole": through, "axis": nax, "sign": sign, "desc": desc})
    return out


def room_extras(geo, spec, d, info):
    """Automatic lights and AI nodes for a room (build only)."""
    x0, y0, z0, x1, y1, z1 = info["interior"]
    bright = spec.get("light", d["light"])
    if bright is True:
        bright = d["light"] or DEFAULTS["light"]
    color = spec.get("light_color", d["light_color"])
    n_lights = 0
    if bright:
        # a third of the way down from the ceiling: lower and the ceiling gets a
        # hot spot, higher and the floor goes dark
        n_lights = auto_lights(geo, info["interior"], float(bright), color, float(d["light_spacing"]),
                               z1 - max(24, (z1 - z0) / 3))
    nodes = 0
    spacing = spec.get("nodes")
    if spacing:
        spacing = 128.0 if spacing is True else max(32.0, float(spacing))
        inset = 32.0

        def axis_points(a, b):
            span = b - a - 2 * inset
            if span < 0:
                return [(a + b) / 2]
            n = int(span // spacing) + 1
            first = a + inset + (span - (n - 1) * spacing) / 2
            return [first + k * spacing for k in range(n)]
        for x in axis_points(x0, x1):
            for y in axis_points(y0, y1):
                add_entity(geo.doc, geo.alloc, "info_node", [("angles", "0 0 0"), ("nodeid", geo.next_node)],
                           (num(x), num(y), z0), color="232 219 8", visgroup=geo.visgroup)
                geo.next_node += 1
                nodes += 1
    return n_lights, nodes


# ---------------------------------------------------------------------------
# corridor
# ---------------------------------------------------------------------------

CORRIDOR_KEYS = {"name", "from", "to", "points", "width", "height", "wall", "floor", "ceiling", "material",
                 "wall_material", "floor_material", "ceiling_material", "from_end", "to_end", "at", "z",
                 "light", "light_color"}


def corridor_between(ra, rb, width, at=None, z=None, where="corridor"):
    """End points (floor centre of each end) of a straight corridor joining two
    room interiors that are side by side along x or y."""
    for axis in (0, 1):
        o = 1 - axis
        if ra[axis + 3] <= rb[axis] + EPS or rb[axis + 3] <= ra[axis] + EPS:
            first, second = (ra, rb) if ra[axis + 3] <= rb[axis] + EPS else (rb, ra)
            ov0, ov1 = max(ra[o], rb[o]), min(ra[o + 3], rb[o + 3])
            if ov1 - ov0 < width - EPS:
                continue
            c = float(at) if at is not None else (ov0 + ov1) / 2
            if c - width / 2 < ov0 - EPS or c + width / 2 > ov1 + EPS:
                raise BuildError(f"{where}: at={num(c)} puts the corridor outside the rooms' shared span "
                                 f"{num(ov0)}..{num(ov1)} on {'xy'[o]}")
            if z is None:
                if abs(ra[2] - rb[2]) > EPS:
                    raise BuildError(f"{where}: the rooms' floors differ (z {num(ra[2])} vs {num(rb[2])}); "
                                     f"give 'z', or join the floors with stairs")
                z = ra[2]
            p0, p1 = [0.0] * 3, [0.0] * 3
            p0[axis], p1[axis] = first[axis + 3], second[axis]
            p0[o] = p1[o] = c
            p0[2] = p1[2] = float(z)
            if first is rb:
                p0, p1 = p1, p0
            return [p0, p1]
    raise BuildError(f"{where}: the two rooms aren't side by side along x or y with {num(width)} units of "
                     f"overlap; give 'points' (axis-aligned segments) instead")


def add_corridor(geo, spec, d, rooms=None, interiors=None, where="corridor", warn=print):
    """A hollow passage along axis-aligned segments between points (floor
    centre of the passage). Each real end is 'open' when it runs into or
    touches a wall or ends inside a known interior (the wall is cut through),
    else capped; `from_end` / `to_end` = open|cap force it."""
    _check_keys(spec, CORRIDOR_KEYS, where, warn)
    rooms = rooms or {}
    interiors = interiors if interiors is not None else []
    if "width" not in spec or "height" not in spec:
        raise BuildError(f"{where}: needs 'width' and 'height' (inside)")
    width, height = float(spec["width"]), float(spec["height"])
    w, f, c = _thick(spec, d, "wall"), _thick(spec, d, "floor"), _thick(spec, d, "ceiling")
    if "points" in spec:
        pts = [_vec(p, 3, f"{where}.points[{i}]") for i, p in enumerate(spec["points"])]
    elif "from" in spec and "to" in spec:
        a, b = spec["from"], spec["to"]
        if isinstance(a, str) and isinstance(b, str):
            for n in (a, b):
                if n not in rooms:
                    raise BuildError(f"{where}: no room named {n!r}")
            pts = corridor_between(rooms[a], rooms[b], width, spec.get("at"), spec.get("z"), where)
        else:
            pts = [_vec(a, 3, where + ".from"), _vec(b, 3, where + ".to")]
    else:
        raise BuildError(f"{where}: needs 'from' and 'to' (points or room names) or 'points'")
    if len(pts) < 2:
        raise BuildError(f"{where}: needs at least 2 points")
    modes = [str(spec.get("from_end", "auto")).lower(), str(spec.get("to_end", "auto")).lower()]
    for m in modes:
        if m not in ("auto", "open", "cap"):
            raise BuildError(f"{where}: from_end / to_end must be auto, open or cap, got {m!r}")
    wm, fm, cm = _mat(spec, d, "wall"), _mat(spec, d, "floor"), _mat(spec, d, "ceiling")
    segs, ends = [], []
    nseg = len(pts) - 1
    for i in range(nseg):
        a, b = pts[i], pts[i + 1]
        diff = [k for k in range(3) if abs(a[k] - b[k]) > EPS]
        if len(diff) != 1 or diff[0] == 2:
            raise BuildError(f"{where}: segment {fmt_pt(a)} -> {fmt_pt(b)} must run along x or y only "
                             f"(same z; use stairs between heights)")
        axis = diff[0]
        o = 1 - axis
        sign = 1 if b[axis] > a[axis] else -1
        s0, s1 = a[axis], b[axis]
        if i > 0:
            s0 -= sign * width / 2       # joints: overlap the corner square
        if i < nseg - 1:
            s1 += sign * width / 2
        lo_a, hi_a = min(s0, s1), max(s0, s1)
        z = a[2]
        cen = a[o]
        lo, hi = [0.0] * 3, [0.0] * 3
        lo[axis], hi[axis] = lo_a, hi_a
        lo[o], hi[o] = cen - width / 2, cen + width / 2
        lo[2], hi[2] = z, z + height
        inner = mkbox(lo, hi)
        # which end of the box is the corridor's from / to end (or a joint)
        end_specs = []
        for at_lo in (True, False):
            is_from = (at_lo == (sign > 0))
            idx = 0 if is_from else 1
            real = (i == 0) if is_from else (i == nseg - 1)
            end_specs.append((at_lo, modes[idx] if real else "joint", "from" if is_from else "to"))
        states = {}
        for at_lo, mode, label in end_specs:
            esign = -1 if at_lo else 1
            if mode in ("open", "cap"):
                states[at_lo] = mode
                continue
            if mode == "joint":
                states[at_lo] = "joint"
                continue
            face = inner[axis] if at_lo else inner[axis + 3]
            near = list(inner)
            if at_lo:
                near[axis + 3] = min(inner[axis + 3], face + max(w, 1))
            else:
                near[axis] = max(inner[axis], face - max(w, 1))
            probe = list(inner)
            probe[axis], probe[axis + 3] = (face - 1, face) if at_lo else (face, face + 1)
            beyond = [(inner[k] + inner[k + 3]) / 2 for k in range(3)]
            beyond[axis] = face + esign * 1
            beyond[2] = z + min(height / 2, 32)
            if geo.overlapping(tuple(near)) or geo.overlapping(tuple(probe)) or \
                    any(point_in(beyond, ib) for ib in interiors):
                states[at_lo] = "open"
            else:
                states[at_lo] = "cap"
        cut = list(inner)
        for at_lo in (True, False):
            if states[at_lo] == "open":
                grown = geo.punch(tuple(cut), axis, -1 if at_lo else 1)
                cut[axis if at_lo else axis + 3] = grown[axis if at_lo else axis + 3]
        geo.carve(tuple(cut))
        shell = []
        olo, ohi = cen - width / 2 - w, cen + width / 2 + w

        def bx(a0, a1, o0, o1, zz0, zz1):
            l, h = [0.0] * 3, [0.0] * 3
            l[axis], h[axis] = a0, a1
            l[o], h[o] = o0, o1
            l[2], h[2] = zz0, zz1
            return mkbox(l, h)
        if f:
            shell.append((bx(lo_a, hi_a, olo, ohi, z - f, z), fm))
        if c:
            shell.append((bx(lo_a, hi_a, olo, ohi, z + height, z + height + c), cm))
        if w:
            shell.append((bx(lo_a, hi_a, olo, cen - width / 2, z, z + height), wm))
            shell.append((bx(lo_a, hi_a, cen + width / 2, ohi, z, z + height), wm))
            for at_lo in (True, False):
                if states[at_lo] in ("cap", "joint"):
                    if at_lo:
                        shell.append((bx(lo_a - w, lo_a, olo, ohi, z - f, z + height + c), wm))
                    else:
                        shell.append((bx(hi_a, hi_a + w, olo, ohi, z - f, z + height + c), wm))
        added = 0
        for box, mat in shell:
            pieces = [box]
            for ib in interiors:
                pieces = [q for p in pieces for q in subtract(p, ib)]
            for p in pieces:
                geo.add_box(p, mat)
                added += 1
        interiors.append(inner)
        segs.append({"interior": inner, "axis": axis, "brushes": added, "cut": cut})
        for at_lo, mode, label in end_specs:
            if mode != "joint":
                esign = -1 if at_lo else 1
                # where a player walking out of this end would be: just past the
                # corridor's own end, and just past the wall it was cut through
                past = []
                for face in ((inner[axis] if at_lo else inner[axis + 3]), (cut[axis] if at_lo else cut[axis + 3])):
                    p = [(inner[k] + inner[k + 3]) / 2 for k in range(3)]
                    p[axis] = face + esign * 2
                    p[2] = z + min(height / 2, 32)
                    past.append(p)
                ends.append({"end": label, "state": states[at_lo], "inner": inner, "past": past})
    ends.sort(key=lambda e: e["end"] != "from")
    return {"segments": segs, "ends": ends, "points": pts, "width": width, "height": height}


def corridor_extras(geo, spec, d, info):
    bright = spec.get("light", d["corridor_light"])
    if bright is True:
        bright = d["corridor_light"] or DEFAULTS["corridor_light"]
    if not bright:
        return 0
    color = spec.get("light_color", d["light_color"])
    n = 0
    for s in info["segments"]:
        b = s["interior"]
        n += auto_lights(geo, b, float(bright), color, float(d["light_spacing"]), b[5] - max(24, (b[5] - b[2]) / 3))
    return n


# ---------------------------------------------------------------------------
# stairs
# ---------------------------------------------------------------------------

STAIRS_KEYS = {"name", "start", "dir", "steps", "top", "rise", "run", "width", "landing", "material",
               "clip", "base"}


def add_stairs(geo, spec, d, where="stairs", warn=print):
    """Solid steps going `dir` from `start` (floor centre of the first step's
    front edge), each a box from the base up to its tread, an optional landing
    at the top, and a player-clip ramp touching every step's nosing."""
    _check_keys(spec, STAIRS_KEYS, where, warn)
    if "start" not in spec or "dir" not in spec or "width" not in spec:
        raise BuildError(f"{where}: needs 'start', 'dir' and 'width'")
    sx = _vec(spec["start"], 3, where + ".start")
    dname = str(spec["dir"]).lower()
    if dname not in DIRS:
        raise BuildError(f"{where}: 'dir' must be +x, -x, +y or -y, got {dname!r}")
    axis, sign = DIRS[dname]
    o = 1 - axis
    rise, run, width = float(spec.get("rise", 8)), float(spec.get("run", 12)), float(spec["width"])
    if "steps" in spec:
        steps = int(spec["steps"])
    elif "top" in spec:
        dz = float(spec["top"]) - sx[2]
        steps = max(1, int(round(dz / rise)))
        rise = dz / steps
        if abs(rise - round(rise)) > EPS:
            warn(f"{where}: {num(dz)} units in {steps} steps makes a {num(rise)} rise (not whole units)")
    else:
        raise BuildError(f"{where}: needs 'steps' or 'top'")
    if steps < 1 or rise <= 0 or run <= 0 or width <= 0:
        raise BuildError(f"{where}: steps, rise, run and width must be positive")
    if rise > 18:
        warn(f"{where}: rise {num(rise)} is higher than the player can step (18)")
    base = float(spec.get("base", sx[2]))
    landing = float(spec.get("landing", 0))
    mat = spec.get("material") or d["stairs_material"]

    def world(u, across, z):
        p = [0.0] * 3
        p[axis] = sx[axis] + sign * u
        p[o] = sx[o] + across
        p[2] = z
        return tuple(num(v) for v in p)

    def bx(u0, u1, z0, z1):
        return mkbox(world(u0, -width / 2, z0), world(u1, width / 2, z1))
    n = 0
    for i in range(steps):
        geo.add_box(bx(i * run, (i + 1) * run, base, sx[2] + (i + 1) * rise), mat)
        n += 1
    top = sx[2] + steps * rise
    if landing > 0:
        geo.add_box(bx(steps * run, steps * run + landing, base, top), mat)
        n += 1
    clip = spec.get("clip", True)
    if clip:
        # cross-section in (along, z): front foot one run before the first
        # step, the slope through every nosing, flat over the last tread
        q = [(-run, sx[2]), (steps * run, sx[2]), (steps * run, top), ((steps - 1) * run, top)]
        half = width / 2
        left = [world(u, -half, z) for u, z in q]
        right = [world(u, half, z) for u, z in q]
        faces = [left, right]
        for k in range(4):
            u0, u1 = k, (k + 1) % 4
            faces.append([left[u0], left[u1], right[u1], right[u0]])
        cm = clip if isinstance(clip, str) else d["clip_material"]
        geo.add_solid(make_convex(faces, cm, geo.alloc, color="220 0 220", visgroup=geo.visgroup))
        n += 1
    end = world(steps * run + landing, 0, top)
    return {"brushes": n, "steps": steps, "rise": rise, "top": top, "end": end}


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

TOP_KEYS = {"defaults", "world", "visgroup", "rooms", "corridors", "stairs", "boxes", "carve", "shapes",
            "entities", "views", "prefabs", "place"}
PREFAB_KEYS = {"boxes", "shapes", "entities", "comment"}
PLACE_KEYS = {"prefab", "origin", "yaw", "name", "repeat", "vars", "comment"}


def _substitute(v, names):
    """Replace {key} placeholders in every string of a JSON value."""
    if isinstance(v, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(names[m.group(1)]) if m.group(1) in names else m.group(0), v)
    if isinstance(v, list):
        return [_substitute(x, names) for x in v]
    if isinstance(v, dict):
        return {k: _substitute(x, names) for k, x in v.items()}
    return v


def expand_prefabs(spec, warn):
    """Turn the layout's 'place' entries into extra shape and entity specs.

    A prefab is a group of boxes / shapes / entities in local coordinates (its
    origin is 0 0 0). Each placement moves it to 'origin', turns it by 'yaw'
    about that origin, and fills {name} (the placement's name), {i} (its index,
    from 1, across 'repeat') and any 'vars' into every string, so targetnames
    and I/O targets stay unique per copy. Boxes become rotatable box shapes."""
    import vmf_shapes
    prefabs = spec.get("prefabs") or {}
    if not isinstance(prefabs, dict):
        raise BuildError("'prefabs' wants an object of name -> {boxes, shapes, entities}")
    shapes, ents, count = [], [], 0
    for pi, pl0 in enumerate(_list(spec, "place")):
        where = f"place[{pi}]"
        _check_keys(pl0, PLACE_KEYS, where, warn)
        pname = pl0.get("prefab")
        if pname not in prefabs:
            raise BuildError(f"{where}: no prefab named {pname!r}")
        pf = prefabs[pname]
        _check_keys(pf, PREFAB_KEYS, f"prefabs['{pname}']", warn)
        for k, pl in enumerate(vmf_shapes.expand_repeat(pl0, where), start=1):
            org = _vec(pl.get("origin", [0, 0, 0]), 3, where + ".origin")
            yaw = float(pl.get("yaw", 0))
            names = {"i": k}
            names.update({kk: vv for kk, vv in (pl.get("vars") or {}).items()})
            names["name"] = _substitute(str(pl.get("name", f"{pname}{k}")), names)
            xf = {"yaw": yaw, "origin": org}
            rot = vmf_shapes._rotation({"yaw": yaw})

            def place_pt(p):
                q = vmf_shapes._apply(rot, (0.0, 0.0, 0.0), tuple(p)) if rot else tuple(p)
                return [num(q[0] + org[0]), num(q[1] + org[1]), num(q[2] + org[2])]
            for b in pf.get("boxes") or []:
                s = {"type": "box", "mins": b.get("mins"), "maxs": b.get("maxs")}
                if "size" in b:
                    s["maxs"] = [float(b["mins"][j]) + float(b["size"][j]) for j in range(3)]
                for key in ("material", "entity", "kv", "name", "outputs", "repeat", "origin", "detail"):
                    if key in b:
                        s[key] = b[key]
                s["_xform"] = xf
                shapes.append(_substitute(s, names))
            for s0 in pf.get("shapes") or []:
                if str(s0.get("type", "")).lower() == "terrain":
                    raise BuildError(f"prefabs['{pname}']: terrain can't be placed as a prefab")
                s = dict(s0)
                s["_xform"] = xf
                shapes.append(_substitute(s, names))
            for e0 in pf.get("entities") or []:
                e = _substitute(dict(e0), names)
                if e.get("scatter"):
                    raise BuildError(f"prefabs['{pname}']: 'scatter' isn't supported inside a prefab")
                e["origin"] = place_pt(_vec(e.get("origin", [0, 0, 0]), 3, f"prefabs['{pname}'].origin"))
                if "look_at" in e:
                    e["look_at"] = place_pt(_vec(e["look_at"], 3, f"prefabs['{pname}'].look_at"))
                elif "angles" in e:
                    a = _vec(e["angles"], 3, f"prefabs['{pname}'].angles")
                    e["angles"] = [a[0], (a[1] + yaw) % 360, a[2]]
                elif isinstance(e.get("yaw"), (int, float)):
                    e["yaw"] = (float(e["yaw"]) + yaw) % 360
                elif yaw:
                    e["yaw"] = yaw % 360
                if e.get("repeat"):
                    raise BuildError(f"prefabs['{pname}']: use 'repeat' on the placement, not on prefab entities")
                ents.append(e)
            count += 1
    return shapes, ents, count
BOX_KEYS = {"name", "mins", "maxs", "size", "material", "entity", "repeat", "kv", "outputs"}
CARVE_KEYS = {"name", "mins", "maxs", "size"}
ENTITY_KEYS = {"class", "name", "origin", "room", "angles", "yaw", "pitch", "look_at", "kv", "brightness", "color",
               "model", "models", "repeat", "scatter", "on_terrain", "outputs"}
SCATTER_KEYS = {"count", "mins", "maxs", "polygon", "avoid", "min_dist", "seed"}


def scatter_points(sc, where, warn):
    """Deterministic random (x, y) points inside a rectangle or polygon, at
    least min_dist apart, outside every 'avoid' rectangle / polygon."""
    import random
    import vmf_shapes
    _check_keys(sc, SCATTER_KEYS, where, warn)
    rng = random.Random(int(sc.get("seed", 1)))
    count = int(sc.get("count", 10))
    if "polygon" in sc:
        poly = [tuple(_vec(p, 2, where + ".polygon")) for p in sc["polygon"]]
        bx = (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
        inside = lambda x, y: vmf_shapes._point_in_poly(x, y, poly)  # noqa: E731
    else:
        mn, mx = _vec(sc.get("mins"), 2, where + ".mins"), _vec(sc.get("maxs"), 2, where + ".maxs")
        bx = (min(mn[0], mx[0]), min(mn[1], mx[1]), max(mn[0], mx[0]), max(mn[1], mx[1]))
        inside = lambda x, y: True  # noqa: E731
    avoid = []
    for i, a in enumerate(sc.get("avoid") or []):
        if "polygon" in a:
            avoid.append(("p", [tuple(_vec(p, 2, f"{where}.avoid[{i}]")) for p in a["polygon"]]))
        else:
            mn, mx = _vec(a.get("mins"), 2, f"{where}.avoid[{i}].mins"), _vec(a.get("maxs"), 2, f"{where}.avoid[{i}].maxs")
            avoid.append(("r", (min(mn[0], mx[0]), min(mn[1], mx[1]), max(mn[0], mx[0]), max(mn[1], mx[1]))))
    md = float(sc.get("min_dist", 0))
    pts = []
    for _ in range(count * 60):
        if len(pts) >= count:
            break
        x, y = rng.uniform(bx[0], bx[2]), rng.uniform(bx[1], bx[3])
        if not inside(x, y):
            continue
        if any((k == "r" and s[0] <= x <= s[2] and s[1] <= y <= s[3]) or
               (k == "p" and vmf_shapes._point_in_poly(x, y, s)) for k, s in avoid):
            continue
        if md and any((x - px) ** 2 + (y - py) ** 2 < md * md for px, py in pts):
            continue
        pts.append((x, y))
    if len(pts) < count:
        warn(f"{where}: only {len(pts)} of {count} points fit (min_dist / avoid too tight)")
    return [(num(x), num(y)) for x, y in pts], rng
VIEW_KEYS = {"name", "pos", "look_at", "pitch", "yaw", "angles", "room", "corner"}


def load_layout(path):
    """JSON, plus whole-line // comments."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as e:
        raise BuildError(f"{path}: invalid JSON: {e}")
    if not isinstance(spec, dict):
        raise BuildError(f"{path}: the layout must be a JSON object")
    return spec


def _list(spec, key):
    v = spec.get(key) or []
    if not isinstance(v, list):
        raise BuildError(f"'{key}' must be a list")
    for i, it in enumerate(v):
        if not isinstance(it, dict):
            raise BuildError(f"{key}[{i}]: want an object")
    return v


def _angles(spec, pos, where):
    if "look_at" in spec:
        return look_angles(pos, _vec(spec["look_at"], 3, where + ".look_at"))
    if "angles" in spec:
        return tuple(_vec(spec["angles"], 3, where + ".angles"))
    if "yaw" in spec or "pitch" in spec:
        return (float(spec.get("pitch", 0)), float(spec.get("yaw", 0)), 0.0)
    return None


def build(doc, spec, warn=print):
    """Generate the layout into `doc` (in memory). Returns a report dict."""
    report = {"rooms": [], "corridors": [], "stairs": [], "warnings": [], "lights": 0, "nodes": 0,
              "views": 0, "entities": 0, "boxes": 0, "carves": 0}

    def w(msg):
        report["warnings"].append(msg)
        warn("WARN " + msg)

    _check_keys(spec, TOP_KEYS, "layout", w)
    _check_keys(spec.get("defaults") or {}, set(DEFAULTS) | {"material"}, "defaults", w)
    d = dict(DEFAULTS)
    d.update(spec.get("defaults") or {})
    if "material" in (spec.get("defaults") or {}):
        for k in ("wall_material", "floor_material", "ceiling_material", "box_material", "stairs_material"):
            if k not in spec["defaults"]:
                d[k] = spec["defaults"]["material"]
    world = doc.top("world")
    for k, v in (spec.get("world") or {}).items():
        world.set_kv(k, str(v))
    start_id = vmf.max_id(doc)
    alloc = vmf.IdAllocator(start_id)
    vg = spec.get("visgroup", "blockout")
    visgroup = ensure_visgroup(doc, vg) if vg else None
    geo = Geo(doc, alloc, visgroup)
    prefab_shapes, prefab_ents, report["placed"] = expand_prefabs(spec, w)
    geo.next_node = 1 + max([int(e.get("nodeid")) for e in vmf.iter_entities(doc)
                             if (e.get("nodeid") or "").isdigit()] or [0])
    rooms, interiors = {}, []
    room_specs = _list(spec, "rooms")
    room_infos = []
    for i, r in enumerate(room_specs):
        name = r.get("name") or f"room{i + 1}"
        where = f"rooms[{i}] '{name}'"
        if name in rooms:
            raise BuildError(f"{where}: duplicate room name")
        # walls first for every room, openings after all rooms exist, so a
        # door between two rooms built back to back goes through both walls
        info = add_room(geo, r, d, where, w, openings=False)
        rooms[name] = info["interior"]
        interiors.append(info["interior"])
        room_infos.append((name, r, info, where))
    for name, r, info, where in room_infos:
        info["openings"] = add_room_openings(geo, r, d, info, where, w)
        report["rooms"].append({"name": name, "interior": info["interior"], "openings": len(info["openings"]),
                                "brushes": info["brushes"]})

    corr_infos = []
    for i, c in enumerate(_list(spec, "corridors")):
        name = c.get("name") or f"corridor{i + 1}"
        where = f"corridors[{i}] '{name}'"
        info = add_corridor(geo, c, d, rooms, interiors, where, w)
        corr_infos.append((name, c, info))
        report["corridors"].append({"name": name, "segments": len(info["segments"]),
                                    "ends": [(e["end"], e["state"]) for e in info["ends"]]})

    for i, cv in enumerate(_list(spec, "carve")):
        where = f"carve[{i}]"
        _check_keys(cv, CARVE_KEYS, where, w)
        b = _interior(cv, where)
        if not geo.carve(b):
            w(f"{where}: {fmt_box(b)} cut nothing")
        report["carves"] += 1

    for i, s in enumerate(_list(spec, "stairs")):
        name = s.get("name") or f"stairs{i + 1}"
        info = add_stairs(geo, s, d, f"stairs[{i}] '{name}'", w)
        report["stairs"].append({"name": name, **info})

    import vmf_shapes
    for i, bspec0 in enumerate(_list(spec, "boxes")):
        where = f"boxes[{i}]"
        _check_keys(bspec0, BOX_KEYS, where, w)
        for bspec in vmf_shapes.expand_repeat(bspec0, where):
            b = _interior(bspec, where)
            mat = bspec.get("material") or d["box_material"]
            cls = bspec.get("entity")
            if cls:
                kvs = [("targetname", bspec["name"])] if bspec.get("name") else []
                if cls.startswith("trigger_"):
                    kvs.append(("spawnflags", "1"))
                    if not bspec.get("material"):
                        mat = "TOOLS/TOOLSTRIGGER"
                for k, v in (bspec.get("kv") or {}).items():
                    kvs = [kv for kv in kvs if kv[0] != k] + [(k, str(v))]
                ent = add_entity(doc, alloc, cls, kvs, None, visgroup=visgroup)
                s = vmf.make_box(b[:3], b[3:], mat, alloc)
                _set_editor(s, editor_block("0 180 137", visgroup))
                ent.insert_child_before_editor(s)
                report["io"] = report.get("io", 0) + add_outputs(ent, bspec.get("outputs"), where)
            else:
                if bspec.get("outputs"):
                    raise BuildError(f"{where}: 'outputs' needs an 'entity'")
                geo.add_box(b, mat)
            report["boxes"] += 1

    terrains = []
    report["shapes"] = []
    own_shapes = _list(spec, "shapes")
    for i, sspec0 in enumerate(own_shapes + prefab_shapes):
        where = (f"shapes[{i}]" if i < len(own_shapes) else f"placed shape #{i - len(own_shapes) + 1}") +             (f" '{sspec0['name']}'" if sspec0.get("name") else "")
        for sspec in vmf_shapes.expand_repeat(sspec0, where):
            rep = vmf_shapes.add_shape(geo, sspec, d, where, w, terrains)
            report["io"] = report.get("io", 0) + rep.get("io", 0)
            if rep.get("slope", 0) > 45.5:
                w(f"{where}: the wedge's slope is {rep['slope']:.1f} deg: too steep to walk up (max ~45)")
            report["shapes"].append({"where": where, **rep})
    report["terrains"] = terrains

    # lights and nodes after all geometry, so they're never carved around
    for name, r, info, where in room_infos:
        nl, nn = room_extras(geo, r, d, info)
        report["lights"] += nl
        report["nodes"] += nn
    for name, c, info in corr_infos:
        report["lights"] += corridor_extras(geo, c, d, info)

    def terrain_z(x, y):
        for (tx0, ty0, tx1, ty1), hfn in terrains:
            if tx0 <= x <= tx1 and ty0 <= y <= ty1:
                return hfn(x, y)
        return None

    own_ents = _list(spec, "entities")
    for i, e0 in enumerate(own_ents + prefab_ents):
        where = f"entities[{i}]" if i < len(own_ents) else f"placed entity #{i - len(own_ents) + 1}"
        _check_keys(e0, ENTITY_KEYS, where, w)
        cls = e0.get("class")
        if not cls:
            raise BuildError(f"{where}: needs 'class'")
        placements = []          # (spec, origin, rng or None)
        if e0.get("scatter"):
            base = _vec(e0.get("origin", [0, 0, 0]), 3, where + ".origin")
            pts, rng = scatter_points(e0["scatter"], where + ".scatter", w)
            for x, y in pts:
                placements.append((e0, [x + base[0], y + base[1], base[2]], rng))
        else:
            for e in vmf_shapes.expand_repeat(e0, where):
                placements.append((e, _vec(e.get("origin", [0, 0, 0]), 3, where + ".origin"), None))
        for e, org, rng in placements:
            if e.get("room"):
                if e["room"] not in rooms:
                    raise BuildError(f"{where}: no room named {e['room']!r}")
                ri = rooms[e["room"]]
                org = [(ri[0] + ri[3]) / 2 + org[0], (ri[1] + ri[4]) / 2 + org[1], ri[2] + org[2]]
            if e.get("on_terrain"):
                tz = terrain_z(org[0], org[1])
                if tz is None:
                    w(f"{where}: ({fmt_pt(org[:2] + [0])}) is on no terrain; z left at {num(org[2])}")
                else:
                    org = [org[0], org[1], tz + org[2]]
            kvs = []
            if e.get("name"):
                kvs.append(("targetname", e["name"]))
            if e.get("yaw") == "random":
                r = rng or __import__("random").Random(len(placements))
                ang = (0, num(r.uniform(0, 360)), 0)
            else:
                ang = _angles(e, org, where)
            if ang is not None:
                if "look_at" in e and cls != "info_target" and not cls.startswith("point_"):
                    ang = (0, ang[1], 0)
                kvs.append(("angles", fmt_angles(ang)))
            if cls == "light":
                kvs += light_kvs(float(e.get("brightness", d["light"] or 300)), e.get("color", d["light_color"]))
            models = e.get("models")
            if models:
                r = rng or __import__("random").Random(hash(tuple(org)) & 0xFFFF)
                kvs.append(("model", r.choice(models)))
            elif e.get("model"):
                kvs.append(("model", e["model"]))
            for k, v in (e.get("kv") or {}).items():
                kvs = [kv for kv in kvs if kv[0] != k] + [(k, v)]
            ent = add_entity(doc, alloc, cls, kvs, tuple(num(v) for v in org),
                             color="0 255 0" if cls == "info_player_start" else "220 30 220", visgroup=visgroup)
            report["io"] = report.get("io", 0) + add_outputs(ent, e.get("outputs"), where)
            report["lights" if cls == "light" else "entities"] += 1

    for i, v in enumerate(_list(spec, "views")):
        where = f"views[{i}]"
        _check_keys(v, VIEW_KEYS, where, w)
        if v.get("room"):
            if v["room"] not in rooms:
                raise BuildError(f"{where}: no room named {v['room']!r}")
            x0, y0, z0, x1, y1, z1 = rooms[v["room"]]
            corner = str(v.get("corner", "sw")).lower()
            if corner not in ("sw", "se", "nw", "ne"):
                raise BuildError(f"{where}: 'corner' must be sw, se, nw or ne")
            inset = 24
            px = x0 + inset if corner[1] == "w" else x1 - inset
            py = y0 + inset if corner[0] == "s" else y1 - inset
            pos = [px, py, z0 + min(64, (z1 - z0) - 16)]
            ang = _angles(v, pos, where) or look_angles(pos, [(x0 + x1) / 2, (y0 + y1) / 2, z0 + 24])
            name = v.get("name") or f"{v['room']}_{corner}"
        else:
            if "pos" not in v:
                raise BuildError(f"{where}: needs 'pos' (eye position) or 'room'")
            pos = _vec(v["pos"], 3, where + ".pos")
            ang = _angles(v, pos, where) or (0, 0, 0)
            name = v.get("name") or f"view{i + 1}"
        name = name if name.startswith("shot_") else "shot_" + name
        add_entity(doc, alloc, "info_target", [("targetname", name), ("angles", fmt_angles(ang))],
                   tuple(num(c) for c in pos), color="255 128 0", visgroup=visgroup)
        report["views"] += 1

    # openings / open ends that lead nowhere make the map leak
    for name, r, info, where in room_infos:
        for k, op in enumerate(info["openings"]):
            h, ax, sg = op["hole"], op["axis"], op["sign"]
            p = [(h[j] + h[j + 3]) / 2 for j in range(3)]
            p[ax] = (h[ax + 3] if sg > 0 else h[ax]) + sg * 2
            if not any(point_in(p, ib) for ib in interiors) and not geo.overlapping(mkbox(p, [c + 0.5 for c in p])):
                w(f"room '{name}' {op['desc']} opens to the void at ({fmt_pt(p)}): the map leaks there "
                  f"unless something seals it")
    for name, c, info in corr_infos:
        for e in info["ends"]:
            if e["state"] == "open" and not any(point_in(p, ib) for p in e["past"] for ib in interiors
                                                if ib is not e["inner"]):
                w(f"corridor '{name}' {e['end']} end is open but leads to no room near "
                  f"({fmt_pt(e['past'][-1])}): it leaks unless something is there")

    bad = [s.get("id") for s in iter_new_solids(doc, start_id) if not solid_ok(s)]
    if bad:
        raise BuildError(f"generated {len(bad)} invalid brush(es) (ids {', '.join(bad[:10])}); nothing written")
    report["added"] = sum(1 for _ in iter_new_solids(doc, start_id))
    report["cut"], report["pieces"], report["skipped"] = geo.cut, geo.pieces, dict(geo.skipped)
    return report


def iter_new_solids(doc, start_id):
    for root in doc.roots:
        if root.name not in ("world", "entity"):
            continue
        for n in vmf.iter_all(root):
            if n.name == "solid" and (n.get("id") or "").isdigit() and int(n.get("id")) > start_id:
                yield n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_skipped(geo):
    for sid, why in sorted(geo.skipped.items(), key=lambda kv: int(kv[0]) if (kv[0] or "").isdigit() else 0):
        print(f"  skipped solid id={sid}: {why} (left as is)")


def _check_new(doc, start_id):
    bad = [s.get("id") for s in iter_new_solids(doc, start_id) if not solid_ok(s)]
    if bad:
        print(f"ABORT: generated invalid brush(es) {', '.join(bad[:10])}; nothing written", file=sys.stderr)
        sys.exit(3)


def _edit(args, fn):
    """Load the map, run fn(geo) -> description, validate, write / preview."""
    doc = vmf.load(args.file)
    src = vmf.serialize(doc)
    start_id = vmf.max_id(doc)
    geo = Geo(doc, vmf.IdAllocator(start_id), detail=getattr(args, "detail", False))
    try:
        desc = fn(geo)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    _check_new(doc, start_id)
    _print_skipped(geo)
    vmf._write_or_preview(doc, args.file, src, args.dry_run, desc)


def _parse_opening(text, typ):
    """'WALL [OFFSET [WIDTH [HEIGHT [SILL]]]]'; OFFSET may be 'c' (centred) or @X (centre at X)."""
    parts = text.replace(",", " ").split()
    if not parts:
        raise BuildError(f"--{typ}: want 'WALL [OFFSET [WIDTH [HEIGHT [SILL]]]]'")
    op = {"wall": parts[0], "type": typ}
    if len(parts) > 1:
        if parts[1].startswith("@"):
            op["at"] = float(parts[1][1:])
        elif parts[1].lower() not in ("c", "center"):
            op["offset"] = float(parts[1])
    for key, val in zip(("width", "height", "sill"), parts[2:5]):
        op[key] = float(val)
    return op


def _mat_args(args):
    out = {}
    for k in ("material", "wall_material", "floor_material", "ceiling_material"):
        v = getattr(args, k, None)
        if v:
            out[k] = v
    for k in ("wall", "floor", "ceiling"):
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    return out


def _cmd_room(args):
    def fn(geo):
        spec = {"mins": args.mins, "maxs": args.maxs, **_mat_args(args)}
        ops = [_parse_opening(t, "door") for t in args.door or []]
        ops += [_parse_opening(t, "window") for t in args.window or []]
        ops += [_parse_opening(t, "hole") for t in args.hole or []]
        spec["openings"] = ops
        if args.roof:
            roof = {"style": args.roof}
            if args.roof_axis:
                roof["axis"] = args.roof_axis
            if args.roof_rise is not None:
                roof["rise"] = args.roof_rise
            spec["roof"] = roof
        info = add_room(geo, spec, DEFAULTS, "room")
        for op in info["openings"]:
            print(f"  {op['desc']}")
        nl = 0
        if args.light:
            nl, _ = room_extras(geo, {"light": args.light}, DEFAULTS, info)
        return (f"room {fmt_box(info['interior'])} (inside): {info['brushes']} brushes, "
                f"{len(info['openings'])} opening(s), {geo.cut} brush(es) cut" + (f", {nl} light(s)" if nl else ""))
    _edit(args, fn)


def _cmd_corridor(args):
    def fn(geo):
        spec = {"from": args.start, "to": args.end, "width": args.width, "height": args.height,
                "from_end": args.from_end, "to_end": args.to_end, **_mat_args(args)}
        if args.via:
            spec = dict(spec)
            spec["points"] = [args.start] + [_vec(v, 3, "--via") for v in args.via] + [args.end]
        info = add_corridor(geo, spec, DEFAULTS)
        nl = corridor_extras(geo, {"light": args.light}, DEFAULTS, info) if args.light else 0
        ends = ", ".join(f"{e['end']} end {e['state']}" for e in info["ends"])
        n = sum(s["brushes"] for s in info["segments"])
        return (f"corridor {len(info['segments'])} segment(s), {n} brushes, {ends}, {geo.cut} brush(es) cut"
                + (f", {nl} light(s)" if nl else ""))
    _edit(args, fn)


def _cmd_stairs(args):
    def fn(geo):
        spec = {"start": args.start, "dir": args.dir, "rise": args.rise, "run": args.run, "width": args.width,
                "landing": args.landing, "clip": not args.no_clip}
        if args.steps:
            spec["steps"] = args.steps
        elif args.top is not None:
            spec["top"] = args.top
        else:
            raise BuildError("give --steps N or --top Z")
        if args.material:
            spec["material"] = args.material
        info = add_stairs(geo, spec, DEFAULTS)
        return (f"stairs: {info['steps']} steps of {num(info['rise'])} up to z {num(info['top'])}, top end at "
                f"({fmt_pt(info['end'])}), {info['brushes']} brushes")
    _edit(args, fn)


def _cmd_carve(args):
    def fn(geo):
        b = mkbox(args.mins, args.maxs)
        n = geo.carve(b)
        return f"carved {fmt_box(b)}: {n} brush(es) cut into {geo.pieces} piece(s)"
    _edit(args, fn)


def _cmd_new_map(args):
    if os.path.exists(args.file) and not args.overwrite:
        print(f"{args.file} exists (pass --overwrite to replace it)", file=sys.stderr)
        sys.exit(1)
    vmf.save(new_document(args.skyname), args.file)
    print(f"empty map written to {args.file}")


def _cmd_build(args):
    exists = os.path.exists(args.file)
    if exists and not (args.append or args.overwrite or args.dry_run):
        print(f"{args.file} exists: pass --append to build into it or --overwrite to replace it", file=sys.stderr)
        sys.exit(1)
    try:
        spec = load_layout(args.layout)
        if exists and args.append:
            doc = vmf.load(args.file)
            src = vmf.serialize(doc)
        else:
            doc = new_document((spec.get("world") or {}).get("skyname", "sky_day01_01"))
            src = None
        report = build(doc, spec)
    except BuildError as e:
        print(f"layout error: {e}", file=sys.stderr)
        sys.exit(2)
    for r in report["rooms"]:
        print(f"  room '{r['name']}' {fmt_box(r['interior'])}: {r['openings']} opening(s)")
    for c in report["corridors"]:
        print(f"  corridor '{c['name']}': {c['segments']} segment(s), "
              + ", ".join(f"{e} end {s}" for e, s in c["ends"]))
    for s in report["stairs"]:
        print(f"  stairs '{s['name']}': {s['steps']} x {num(s['rise'])} up to z {num(s['top'])}, "
              f"top end at ({fmt_pt(s['end'])})")
    kinds = {}
    for s in report.get("shapes", []):
        k = kinds.setdefault(s["type"], [0, 0])
        k[0] += 1
        k[1] += s["brushes"]
        if s["type"] == "terrain":
            print(f"  terrain {s['where']}: {s['tiles']} tiles, {s['displacements']} displacement(s), "
                  f"height {num(s['hmin'])}..{num(s['hmax'])}")
    if kinds:
        print("  shapes: " + ", ".join(f"{n} {t} ({b} brushes)" for t, (n, b) in sorted(kinds.items())))
    for sid, why in report["skipped"].items():
        print(f"  skipped solid id={sid}: {why} (not cut)")
    print(f"brushes: {report['added']} in the map from this build ({report['cut']} cut into "
          f"{report['pieces']} pieces); lights {report['lights']}, views {report['views']}, "
          f"nodes {report['nodes']}, other entities {report['entities']}, I/O {report.get('io', 0)}"
          + (f", prefab copies {report['placed']}" if report.get("placed") else ""))
    if report["warnings"]:
        print(f"{len(report['warnings'])} warning(s) above")
    if args.dry_run:
        print("[dry-run] nothing written")
        return
    if src is not None:
        vmf._write_or_preview(doc, args.file, src, False, f"blockout from {args.layout} built into the map")
    else:
        vmf.save(doc, args.file)
        print(f"written to {args.file}")
    print(f"next: lint {args.file}, render it (--cut Z per floor, --axis x|y), compile, shoot")


def add_parsers(sub):
    def geom(sp, room=True):
        sp.add_argument("--wall", type=float, help="wall thickness (default %d)" % DEFAULTS["wall"])
        sp.add_argument("--floor", type=float, help="floor thickness (default %d)" % DEFAULTS["floor"])
        sp.add_argument("--ceiling", type=float, help="ceiling thickness (default %d)" % DEFAULTS["ceiling"])
        sp.add_argument("--material", help="material for every surface")
        sp.add_argument("--wall-material", dest="wall_material")
        sp.add_argument("--floor-material", dest="floor_material")
        sp.add_argument("--ceiling-material", dest="ceiling_material")
        sp.add_argument("--light", type=float, metavar="BRIGHTNESS", help="add ceiling lights (e.g. 300)")
        sp.add_argument("--dry-run", action="store_true", help="show a diff, write nothing")

    xyz = dict(nargs=3, type=float, metavar=("X", "Y", "Z"))

    sp = sub.add_parser("room", help="hollow box around an interior (floor/ceiling/walls) with doors and windows")
    sp.add_argument("file")
    sp.add_argument("--mins", required=True, **xyz, help="interior min corner (floor level)")
    sp.add_argument("--maxs", required=True, **xyz, help="interior max corner (ceiling level)")
    sp.add_argument("--door", action="append", metavar="'WALL [OFFSET [W [H]]]'",
                    help="WALL north|south|east|west (+y/-y/+x/-x); OFFSET from the wall's inside min corner, "
                         "'c' = centred, @X = centred at X; default 64 x 112")
    sp.add_argument("--window", action="append", metavar="'WALL [OFFSET [W [H [SILL]]]]'",
                    help="like --door; default 96 x 64, sill 48")
    sp.add_argument("--hole", action="append", metavar="'WALL OFFSET W H [SILL]'", help="any opening")
    sp.add_argument("--roof", choices=("gable", "shed", "barrel", "flat"),
                    help="put a roof on the walls (with --ceiling 0 the roof closes the room)")
    sp.add_argument("--roof-axis", choices=("x", "y"), help="ridge direction (default: the room's long side)")
    sp.add_argument("--roof-rise", type=float, help="ridge height above the eaves (default: a quarter of the span)")
    geom(sp)
    sp.set_defaults(func=_cmd_room)

    sp = sub.add_parser("corridor", help="hollow passage between two floor points; opens into the walls it reaches")
    sp.add_argument("file")
    sp.add_argument("--from", dest="start", required=True, **xyz, help="floor centre of one end")
    sp.add_argument("--to", dest="end", required=True, **xyz, help="floor centre of the other end")
    sp.add_argument("--via", action="append", metavar="'X Y Z'", help="corner point (repeatable)")
    sp.add_argument("--width", type=float, required=True, help="inside width")
    sp.add_argument("--height", type=float, required=True, help="inside height")
    sp.add_argument("--from-end", choices=("auto", "open", "cap"), default="auto")
    sp.add_argument("--to-end", choices=("auto", "open", "cap"), default="auto")
    geom(sp)
    sp.set_defaults(func=_cmd_corridor)

    sp = sub.add_parser("stairs", help="solid steps + player-clip ramp from a start point in a direction")
    sp.add_argument("file")
    sp.add_argument("--start", required=True, **xyz, help="floor centre of the first step's front edge")
    sp.add_argument("--dir", required=True, choices=sorted(DIRS), help="climbing direction")
    sp.add_argument("--steps", type=int)
    sp.add_argument("--top", type=float, help="instead of --steps: climb to this z (rise adjusted)")
    sp.add_argument("--rise", type=float, default=8)
    sp.add_argument("--run", type=float, default=12)
    sp.add_argument("--width", type=float, required=True)
    sp.add_argument("--landing", type=float, default=0, help="flat platform this deep after the last step")
    sp.add_argument("--material")
    sp.add_argument("--no-clip", action="store_true", help="no player-clip ramp")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_stairs)

    sp = sub.add_parser("carve", help="cut an axis-aligned box out of the axis-aligned world brushes it overlaps")
    sp.add_argument("file")
    sp.add_argument("--mins", required=True, **xyz)
    sp.add_argument("--maxs", required=True, **xyz)
    sp.add_argument("--detail", action="store_true", help="also cut func_detail brushes")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_carve)

    sp = sub.add_parser("build", help="generate a blockout (rooms, corridors, stairs, entities) from a JSON layout")
    sp.add_argument("layout", help="LAYOUT.json (format: SKILL.md 'Blockouts')")
    sp.add_argument("file", help="map to write: created, or with --append built into")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--append", action="store_true", help="build into the existing map")
    g.add_argument("--overwrite", action="store_true", help="replace the existing map with a fresh one")
    sp.add_argument("--dry-run", action="store_true", help="build in memory and report, write nothing")
    sp.set_defaults(func=_cmd_build)

    sp = sub.add_parser("new-map", help="write an empty map (world only)")
    sp.add_argument("file")
    sp.add_argument("--skyname", default="sky_day01_01")
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=_cmd_new_map)
