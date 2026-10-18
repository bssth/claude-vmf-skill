#!/usr/bin/env python3
"""vmf_render.py - draw a VMF map as a PNG so it can be looked at.

`python vmf.py render MAP.vmf -o plan.png` draws a plan (top) view, or a side
view with --axis x|y. Brushes are filled by kind (world, detail, brush entity,
displacement, water) and shaded by height/depth; tool brushes (trigger, clip,
sky, areaportal) are outlined; point entities get markers and labels; a
coordinate grid with labelled lines lets you read world positions back off the
image. Brushes whose planes don't bound a volume are drawn in red.

Stdlib only: a small RGB canvas with scanline polygon fill, Bresenham lines, a
5x7 bitmap font and a zlib PNG encoder.
"""

from __future__ import annotations

import math
import os
import struct
import sys
import zlib

import vmf

# ---------------------------------------------------------------------------
# Canvas + PNG
# ---------------------------------------------------------------------------

# 5x7 font, ASCII 32..126, 5 column bytes per glyph, bit 0 = top row.
_FONT = bytes.fromhex(
    "0000000000" "00005f0000" "0007000700" "147f147f14" "242a7f2a12" "2313086462" "3649552250" "0005030000"
    "001c224100" "0041221c00" "082a1c2a08" "08083e0808" "0050300000" "0808080808" "0060600000" "2010080402"
    "3e5149453e" "00427f4000" "4261514946" "2141454b31" "1814127f10" "2745454539" "3c4a494930" "0171090503"
    "3649494936" "064949291e" "0036360000" "0056360000" "0814224100" "1414141414" "0041221408" "0201510906"
    "324979413e" "7e1111117e" "7f49494936" "3e41414122" "7f4141221c" "7f49494941" "7f09090101" "3e41415132"
    "7f0808087f" "00417f4100" "2040413f01" "7f08142241" "7f40404040" "7f0204027f" "7f0408107f" "3e4141413e"
    "7f09090906" "3e4151215e" "7f09192946" "4649494931" "01017f0101" "3f4040403f" "1f2040201f" "7f2018207f"
    "6314081463" "0304780403" "6151494543" "007f414100" "0204081020" "0041417f00" "0402010204" "4040404040"
    "0001020400" "2054545478" "7f48444438" "3844444420" "384444487f" "3854545418" "087e090102" "0c5252523e"
    "7f08040478" "00447d4000" "2040443d00" "7f10284400" "00417f4000" "7c04180478" "7c08040478" "3844444438"
    "7c14141408" "081414187c" "7c08040408" "4854545420" "043f444020" "3c4040207c" "1c2040201c" "3c4030403c"
    "4428102844" "0c5050503c" "4464544c44" "0008364100" "00007f0000" "0041360800" "0201020402"
)
GLYPH_W, GLYPH_H = 6, 7  # advance (5 px + 1 gap) and height


def text_size(s: str, scale: int):
    return max(0, len(s) * GLYPH_W * scale - scale), GLYPH_H * scale


def _clip_segment(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
    """Liang-Barsky: clip a segment to a rectangle; None if fully outside."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return None
                t0 = max(t0, t)
            else:
                if t < t0:
                    return None
                t1 = min(t1, t)
    return x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy


class Canvas:
    """RGB8 image in a bytearray. Colors are 3-byte `bytes`."""

    def __init__(self, w: int, h: int, bg: bytes):
        self.w, self.h = w, h
        self.buf = bytearray(bg * (w * h))

    def span(self, y, x0, x1, c):
        if y < 0 or y >= self.h:
            return
        x0 = max(x0, 0)
        x1 = min(x1, self.w - 1)
        if x1 < x0:
            return
        i = (y * self.w + x0) * 3
        self.buf[i:i + (x1 - x0 + 1) * 3] = c * (x1 - x0 + 1)

    def hatch_span(self, y, x0, x1, c, step):
        """Every `step`-th pixel on a diagonal pattern: a cheap see-through fill."""
        if y < 0 or y >= self.h:
            return
        x0 = max(x0, 0)
        x1 = min(x1, self.w - 1)
        first = x0 + ((-(x0 + y)) % step)
        if first > x1:
            return
        cnt = (x1 - first) // step + 1
        base = (y * self.w + first) * 3
        stride = step * 3
        for ch in range(3):
            s = base + ch
            self.buf[s:s + (cnt - 1) * stride + 1:stride] = bytes([c[ch]]) * cnt

    def px(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.buf[i:i + 3] = c

    def rect(self, x0, y0, x1, y1, c):
        for y in range(max(y0, 0), min(y1, self.h - 1) + 1):
            self.span(y, x0, x1, c)

    def frame(self, x0, y0, x1, y1, c):
        self.span(y0, x0, x1, c)
        self.span(y1, x0, x1, c)
        for y in range(max(y0, 0), min(y1, self.h - 1) + 1):
            self.px(x0, y, c)
            self.px(x1, y, c)

    def disc(self, cx, cy, r, c):
        for dy in range(-r, r + 1):
            dx = int(math.sqrt(max(0.0, r * r - dy * dy)) + 0.5)
            self.span(cy + dy, cx - dx, cx + dx, c)

    def ring(self, cx, cy, r, c, width=1):
        ri = max(0, r - width)
        for dy in range(-r, r + 1):
            xo = int(math.sqrt(max(0.0, r * r - dy * dy)) + 0.5)
            if abs(dy) < ri:
                xi = int(math.sqrt(max(0.0, ri * ri - dy * dy)) + 0.5)
                self.span(cy + dy, cx - xo, cx - xi, c)
                self.span(cy + dy, cx + xi, cx + xo, c)
            else:
                self.span(cy + dy, cx - xo, cx + xo, c)

    def line(self, x0, y0, x1, y1, c, width=1, dash=0):
        m = width + 1
        seg = _clip_segment(x0, y0, x1, y1, -m, -m, self.w - 1 + m, self.h - 1 + m)
        if seg is None:
            return
        x0, y0, x1, y1 = (int(round(v)) for v in seg)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        r = width // 2
        i = 0
        while True:
            if not dash or (i // dash) % 2 == 0:
                if width == 1:
                    self.px(x0, y0, c)
                else:
                    self.rect(x0 - r, y0 - r, x0 - r + width - 1, y0 - r + width - 1, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            i += 1

    def polyline(self, pts, c, width=1, closed=True, dash=0):
        n = len(pts)
        for i in range(n if closed else n - 1):
            a, b = pts[i], pts[(i + 1) % n]
            self.line(a[0], a[1], b[0], b[1], c, width, dash)

    def fill_poly(self, pts, c, hatch=0):
        """Even-odd scanline fill, sampling pixel centers."""
        n = len(pts)
        if n < 3:
            return
        ys = [p[1] for p in pts]
        y0 = max(0, int(math.ceil(min(ys) - 0.5)))
        y1 = min(self.h - 1, int(math.floor(max(ys) - 0.5)))
        edges = []
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            if a[1] != b[1]:
                edges.append((a[0], a[1], (b[0] - a[0]) / (b[1] - a[1]), min(a[1], b[1]), max(a[1], b[1])))
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            xs = sorted(ax + (yc - ay) * k for ax, ay, k, lo, hi in edges if lo <= yc < hi)
            for j in range(0, len(xs) - 1, 2):
                xa = int(math.ceil(xs[j] - 0.5))
                xb = int(math.floor(xs[j + 1] - 0.5))
                if hatch:
                    self.hatch_span(y, xa, xb, c, hatch)
                else:
                    self.span(y, xa, xb, c)

    def text(self, x, y, s, c, scale=1, bg=None, pad=1):
        w, h = text_size(s, scale)
        if bg is not None:
            self.rect(x - pad, y - pad, x + w + pad - 1, y + h + pad - 1, bg)
        for i, ch in enumerate(s):
            o = ord(ch) - 32
            if not 0 <= o < 95:
                o = ord("?") - 32
            gx = x + i * GLYPH_W * scale
            for cx in range(5):
                bits = _FONT[o * 5 + cx]
                for ry in range(7):
                    if bits >> ry & 1:
                        if scale == 1:
                            self.px(gx + cx, y + ry, c)
                        else:
                            self.rect(gx + cx * scale, y + ry * scale,
                                      gx + cx * scale + scale - 1, y + ry * scale + scale - 1, c)
        return w, h

    def save_png(self, path):
        row = self.w * 3
        raw = b"".join(b"\x00" + bytes(self.buf[y * row:(y + 1) * row]) for y in range(self.h))

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 6))
               + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(png)


def rgb(r, g, b):
    return bytes((max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))))


def shade(c, f):
    return rgb(c[0] * f, c[1] * f, c[2] * f)


# ---------------------------------------------------------------------------
# Map model for drawing
# ---------------------------------------------------------------------------

_SKY = {"TOOLS/TOOLSSKYBOX", "TOOLS/TOOLSSKYBOX2D"}
_CLIP = {"TOOLS/TOOLSCLIP", "TOOLS/TOOLSPLAYERCLIP", "TOOLS/TOOLSNPCCLIP", "TOOLS/TOOLSINVISIBLE",
         "TOOLS/TOOLSINVISIBLELADDER", "TOOLS/TOOLSGRENADECLIP", "TOOLS/TOOLSDRONECLIP"}
_TRIGGER = {"TOOLS/TOOLSTRIGGER"}
_HINT = {"TOOLS/TOOLSHINT", "TOOLS/TOOLSSKIP"}
_PORTAL = {"TOOLS/TOOLSAREAPORTAL", "TOOLS/TOOLSOCCLUDER"}
_NODRAW = {"TOOLS/TOOLSNODRAW"}

# kind -> (color, style); style: fill | outline | dashed | hatch
KINDS = {
    "world":    (rgb(150, 150, 150), "fill"),
    "nodraw":   (rgb(118, 108, 96), "fill"),
    "detail":   (rgb(122, 142, 170), "fill"),
    "brushent": (rgb(70, 168, 160), "fill"),
    "disp":     (rgb(146, 156, 98), "fill"),
    "water":    (rgb(60, 112, 205), "fill"),
    "sky":      (rgb(110, 190, 255), "outline"),
    "portal":   (rgb(60, 225, 225), "outline"),
    "clip":     (rgb(195, 95, 225), "dashed"),
    "hint":     (rgb(130, 130, 130), "dashed"),
    "trigger":  (rgb(255, 150, 40), "hatch"),
    "invalid":  (rgb(255, 40, 40), "outline"),
}

BG = rgb(22, 24, 29)
GRID = rgb(38, 42, 50)
GRID_MAJOR = rgb(58, 64, 76)
TEXT = rgb(235, 235, 235)
TEXT_BG = rgb(16, 17, 20)
HIGHLIGHT = rgb(255, 60, 220)

# entity kind -> color
EKINDS = {
    "player": rgb(70, 230, 90),
    "npc": rgb(255, 70, 60),
    "node": rgb(105, 135, 160),
    "prop": rgb(190, 140, 80),
    "light": rgb(255, 225, 70),
    "item": rgb(60, 210, 190),
    "camera": rgb(250, 250, 250),
    "logic": rgb(160, 160, 175),
    "point": rgb(200, 200, 210),
}


def solid_kind(solid, cls):
    sides = list(solid.children("side"))
    mats = [(s.get("material") or "").upper() for s in sides]

    def all_in(group):
        return bool(mats) and all(m in group for m in mats)

    if cls.startswith("trigger_") or all_in(_TRIGGER):
        return "trigger"
    if all_in(_SKY):
        return "sky"
    if all_in(_HINT):
        return "hint"
    if cls in ("func_areaportal", "func_areaportalwindow", "func_occluder") or all_in(_PORTAL):
        return "portal"
    if all_in(_CLIP) or cls in ("func_clip_vphysics", "func_ladder"):
        return "clip"
    if any(s.first("dispinfo") is not None for s in sides):
        return "disp"
    if any("WATER" in m for m in mats) and cls in ("worldspawn", "func_detail", "func_water_analog"):
        return "water"
    if cls == "worldspawn":
        return "nodraw" if all_in(_NODRAW) else "world"
    if cls == "func_detail":
        return "detail"
    return "brushent"


def entity_kind(ent):
    cls = (ent.get("classname") or "").lower()
    name = (ent.get("targetname") or "").lower()
    if cls.startswith("info_player_"):
        return "player"
    if cls.startswith("npc_") or cls.startswith("monster_"):
        return "npc"
    if cls.startswith("info_node") or cls == "info_hint":
        return "node"
    if cls.startswith("prop_"):
        return "prop"
    if cls == "point_viewcontrol" or name.startswith("shot_"):
        return "camera"
    if cls.startswith("light") or cls in ("env_projectedtexture", "point_spotlight"):
        return "light"
    if cls.startswith("weapon_") or cls.startswith("item_"):
        return "item"
    if cls.startswith("logic_") or cls.startswith("math_") or cls.startswith("filter_") \
            or cls in ("ai_script_conditions", "point_template", "env_global", "game_text"):
        return "logic"
    return "point"


def parse_vec(s, n=3):
    try:
        v = tuple(float(c) for c in (s or "").split())
    except ValueError:
        return None
    return v if len(v) == n else None


def forward_vector(pitch, yaw):
    """Source angles (pitch down-positive, yaw CCW from +X) -> unit forward."""
    p, y = math.radians(pitch), math.radians(yaw)
    return (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))


# axis -> (u index, v index, depth sign/index, label). depth grows toward camera.
#   z: looking down from +Z: u = x (right), v = y (up)
#   x: looking from +X toward -X: u = y, v = z
#   y: looking from -Y toward +Y: u = x, v = z
_AXES = {
    "z": (0, 1, 2, +1, "top view (x right, y up), looking down -Z"),
    "x": (1, 2, 0, +1, "side view (y right, z up), looking toward -X"),
    "y": (0, 2, 1, -1, "side view (x right, z up), looking toward +Y"),
}


def _hull(points):
    """Andrew's monotone chain; points are (x, y) tuples."""
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


class Brush:
    __slots__ = ("solid", "ent", "cls", "kind", "valid", "mins", "maxs", "hull", "near", "id")

    def __init__(self, solid, ent, cls, kind, valid, pts, axis):
        ui, vi, di, ds, _ = _AXES[axis]
        self.solid, self.ent, self.cls, self.kind, self.valid = solid, ent, cls, kind, valid
        self.id = solid.get("id")
        self.mins = tuple(min(p[i] for p in pts) for i in range(3))
        self.maxs = tuple(max(p[i] for p in pts) for i in range(3))
        self.hull = _hull([(p[ui], p[vi]) for p in pts])
        self.near = max(ds * p[di] for p in pts)


def gather_brushes(doc, axis, prefer_editor_verts=False):
    """Every compiled brush (world + brush entities; `hidden` blocks are skipped
    because vbsp ignores them) as a Brush. Returns (brushes, unusable_count)."""
    out, bad = [], 0
    owners = []
    world = doc.top("world")
    if world is not None:
        owners.append((world, None, "worldspawn"))
    for e in vmf.iter_entities(doc):
        if e.first("solid") is not None:
            owners.append((e, e, e.get("classname") or ""))
    for node, ent, cls in owners:
        for solid in node.children("solid"):
            pts, valid = vmf.solid_vertices(solid, prefer_editor_verts)
            if not pts:
                bad += 1
                continue
            kind = solid_kind(solid, cls) if valid else "invalid"
            out.append(Brush(solid, ent, cls, kind, valid, pts, axis))
    return out, bad


def skybox_cluster(brushes, cam, gap=256.0):
    """Indices of brushes that belong to the 3D skybox: along any axis, the
    interval cluster holding the sky_camera when it isn't the biggest cluster."""
    excluded = set()
    for ax in range(3):
        order = sorted(range(len(brushes)), key=lambda i: brushes[i].mins[ax])
        clusters = []
        for i in order:
            lo, hi = brushes[i].mins[ax], brushes[i].maxs[ax]
            if clusters and lo <= clusters[-1][1] + gap:
                clusters[-1][1] = max(clusters[-1][1], hi)
                clusters[-1][2].append(i)
            else:
                clusters.append([lo, hi, [i]])
        if len(clusters) < 2:
            continue
        home = next((c for c in clusters if c[0] - gap <= cam[ax] <= c[1] + gap), None)
        biggest = max(clusters, key=lambda c: len(c[2]))
        if home is not None and home is not biggest:
            excluded.update(home[2])
    return excluded


def read_pointfile(path):
    pts = []
    with open(path, "r", encoding="latin-1") as f:
        for line in f:
            v = parse_vec(line)
            if v:
                pts.append(v)
    return pts


# ---------------------------------------------------------------------------
# render command
# ---------------------------------------------------------------------------

def _nice_step(units_per_px, min_px=70):
    step = 16
    while step / units_per_px < min_px:
        step *= 2
    return step


def _cmd_render(args):
    doc = vmf.load(args.file)
    axis = args.axis
    ui, vi, di, ds, axis_desc = _AXES[axis]

    brushes, unusable = gather_brushes(doc, axis, args.editor_verts)

    # point entities with an origin (brush entities are drawn via their solids)
    points = []
    for e in vmf.iter_entities(doc):
        if e.first("solid") is not None:
            continue
        o = parse_vec(e.get("origin"))
        if o is not None:
            points.append((e, o))

    # slice along the view axis, in world coordinates of that axis
    lo, hi = -math.inf, math.inf
    if args.slice:
        lo, hi = min(args.slice), max(args.slice)
    if args.cut is not None:
        hi = args.cut

    def in_slice_range(a, b):
        return b >= lo and a <= hi

    brushes = [b for b in brushes if in_slice_range(b.mins[di], b.maxs[di])]
    points = [(e, o) for e, o in points if lo <= o[di] <= hi]

    # 3D skybox: drop it from the default framing
    notes = []
    if not args.bbox and not args.keep_skybox and not args.focus:
        cams = [o for e, o in points if (e.get("classname") or "") == "sky_camera"]
        if cams and brushes:
            ex = skybox_cluster(brushes, cams[0])
            if ex and len(ex) < len(brushes):
                bx = [brushes[i] for i in ex]
                smin = tuple(min(b.mins[i] for b in bx) for i in range(3))
                smax = tuple(max(b.maxs[i] for b in bx) for i in range(3))
                brushes = [b for i, b in enumerate(brushes) if i not in ex]
                points = [(e, o) for e, o in points
                          if not all(smin[i] - 64 <= o[i] <= smax[i] + 64 for i in range(3))]
                notes.append(f"3D skybox near sky_camera left out ({len(ex)} brushes); --keep-skybox to draw it")

    # highlight selection (same selectors as find)
    sel_args = argparse_ns(cls=args.cls, targetname=args.targetname, kv=args.kv)
    selecting = any(v is not None for v in (args.cls, args.targetname, args.kv))
    sel_ents = set()
    if selecting:
        for e in vmf.iter_entities(doc):
            if vmf._entity_matches(e, sel_args):
                sel_ents.add(id(e))
        if not sel_ents:
            notes.append("selector matched no entities")

    # overlays
    leak = read_pointfile(args.leak) if args.leak else []
    marks = []
    for m in args.mark or []:
        coords, _, label = m.partition(":")
        v = parse_vec(coords.replace(",", " "))
        if v is None:
            print(f"bad --mark {m!r} (want 'x y z[:label]')", file=sys.stderr)
            sys.exit(2)
        marks.append((v, label))
    views = []
    if args.views:
        import vmf_game
        views = vmf_game.parse_views(args.views)

    # framing
    def uv(p):
        return p[ui], p[vi]

    if args.bbox:
        u0, v0, u1, v1 = args.bbox
        u0, u1 = min(u0, u1), max(u0, u1)
        v0, v1 = min(v0, v1), max(v0, v1)
    else:
        us, vs = [], []
        if args.focus:
            for b in brushes:
                if b.ent is not None and id(b.ent) in sel_ents:
                    us += [b.mins[ui], b.maxs[ui]]
                    vs += [b.mins[vi], b.maxs[vi]]
            for e, o in points:
                if id(e) in sel_ents:
                    us.append(o[ui])
                    vs.append(o[vi])
            for p in leak + [m[0] for m in marks] + [v["pos"] for v in views]:
                us.append(p[ui])
                vs.append(p[vi])
            if not us:
                print("--focus: nothing selected to focus on (give --class/--targetname/--kv, "
                      "--leak, --mark or --views)", file=sys.stderr)
                sys.exit(1)
            pad = args.focus
        else:
            for b in brushes:
                us += [b.mins[ui], b.maxs[ui]]
                vs += [b.mins[vi], b.maxs[vi]]
            for e, o in points:
                us.append(o[ui])
                vs.append(o[vi])
            for p in leak + [m[0] for m in marks]:
                us.append(p[ui])
                vs.append(p[vi])
            pad = None
        if not us:
            us, vs = [-512, 512], [-512, 512]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        if pad is None:
            pad = max(64.0, 0.04 * max(u1 - u0, v1 - v0))
        u0, u1, v0, v1 = u0 - pad, u1 + pad, v0 - pad, v1 + pad
    du, dv = max(u1 - u0, 1.0), max(v1 - v0, 1.0)
    size = max(64, min(args.size, 8192))
    s = max(du, dv) / size  # world units per pixel
    W, H = max(1, int(round(du / s))), max(1, int(round(dv / s)))

    def P(u, v):
        return (u - u0) / s, (v1 - v) / s

    def P3(p):
        return P(p[ui], p[vi])

    cv = Canvas(W, H, BG)
    fs = args.font_scale

    # grid
    step = _nice_step(s, 70 * max(1, fs) // 2 + 35)
    gu = math.ceil(u0 / step) * step
    while gu <= u1:
        x = int(round((gu - u0) / s))
        cv.line(x, 0, x, H - 1, GRID_MAJOR if gu == 0 else GRID)
        gu += step
    gv = math.ceil(v0 / step) * step
    while gv <= v1:
        y = int(round((v1 - gv) / s))
        cv.line(0, y, W - 1, y, GRID_MAJOR if gv == 0 else GRID)
        gv += step

    # depth range for shading (only what's in view)
    vis = [b for b in brushes if b.maxs[ui] >= u0 and b.mins[ui] <= u1 and b.maxs[vi] >= v0 and b.mins[vi] <= v1]
    if vis:
        dmin = min(b.near for b in vis)
        dmax = max(b.near for b in vis)
    else:
        dmin = dmax = 0.0
    drange = max(dmax - dmin, 1.0)

    counts = {}
    sky = [b for b in vis if b.kind == "sky"]
    fills = sorted((b for b in vis if KINDS[b.kind][1] == "fill"), key=lambda b: b.near)
    tools = [b for b in vis if KINDS[b.kind][1] in ("dashed", "hatch", "outline")
             and b.kind not in ("sky", "invalid")]
    invalid = [b for b in vis if b.kind == "invalid"]
    if not args.tools:
        tools = [b for b in tools if b.kind != "hint"]

    for b in sky:
        cv.polyline([P(*p) for p in b.hull], KINDS["sky"][0])
    for b in fills:
        col = KINDS[b.kind][0]
        f = 0.5 + 0.5 * (b.near - dmin) / drange
        poly = [P(*p) for p in b.hull]
        cv.fill_poly(poly, shade(col, f))
        cv.polyline(poly, shade(col, f * 0.62))
    for b in tools:
        col, style = KINDS[b.kind]
        poly = [P(*p) for p in b.hull]
        if style == "hatch":
            cv.fill_poly(poly, shade(col, 0.8), hatch=7)
            cv.polyline(poly, col)
        elif style == "dashed":
            cv.polyline(poly, col, dash=4)
        else:
            cv.polyline(poly, col)
    for b in invalid:
        poly = [P(*p) for p in b.hull]
        cv.polyline(poly, KINDS["invalid"][0], width=2)
        if len(poly) >= 2:
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            cv.line(min(xs), min(ys), max(xs), max(ys), KINDS["invalid"][0], 2)
            cv.line(min(xs), max(ys), max(xs), min(ys), KINDS["invalid"][0], 2)
    for b in vis:
        counts[b.kind] = counts.get(b.kind, 0) + 1

    # highlighted brush entities
    for b in vis:
        if b.ent is not None and id(b.ent) in sel_ents:
            cv.polyline([P(*p) for p in b.hull], HIGHLIGHT, width=2)

    # labels are collected and placed last, greedily, without overlaps
    labels = []  # (priority, x, y, text, color)

    def add_label(prio, x, y, text, col):
        labels.append((prio, x, y, text, col))

    # brush entity labels
    if args.labels != "none":
        seen = set()
        for b in vis:
            if b.ent is None or id(b.ent) in seen:
                continue
            seen.add(id(b.ent))
            name = b.ent.get("targetname")
            hl = id(b.ent) in sel_ents
            if not (hl or name or args.labels == "all"):
                continue
            hs = [P(*p) for p in b.hull]
            cx = sum(p[0] for p in hs) / len(hs)
            cy = sum(p[1] for p in hs) / len(hs)
            add_label(0 if hl else 4, cx, cy, name or b.cls, HIGHLIGHT if hl else KINDS[b.kind][0])

    # point entities
    ecounts = {}
    order = {"node": 0, "prop": 1, "logic": 2, "point": 3, "light": 4, "item": 5, "camera": 6, "npc": 7, "player": 8}
    shown = []
    for e, o in points:
        k = entity_kind(e)
        if k == "node" and args.no_nodes:
            continue
        if k == "prop" and args.no_props:
            continue
        x, y = P3(o)
        if not (-20 <= x <= W + 20 and -20 <= y <= H + 20):
            continue
        shown.append((order[k], k, e, o, x, y))
        ecounts[k] = ecounts.get(k, 0) + 1
    shown.sort(key=lambda t: t[0])

    def arrow(x, y, ang, length, col, width=1):
        a = parse_vec(ang)
        if a is None:
            return
        fwd = forward_vector(a[0], a[1])
        du_, dv_ = fwd[ui], fwd[vi]
        ln = math.hypot(du_, dv_)
        if ln < 0.2:
            return
        ex, ey = x + du_ / ln * length, y - dv_ / ln * length
        cv.line(x, y, ex, ey, col, width)
        # arrow head
        ang2 = math.atan2(ey - y, ex - x)
        for da in (2.6, -2.6):
            cv.line(ex, ey, ex + math.cos(ang2 + da) * length * 0.35, ey + math.sin(ang2 + da) * length * 0.35, col, width)

    many = len(shown) > 400
    for _o, k, e, o, x, y in shown:
        xi, yi = int(round(x)), int(round(y))
        col = EKINDS[k]
        hl = id(e) in sel_ents
        name = e.get("targetname")
        cls = e.get("classname") or ""
        if k == "node":
            cv.rect(xi - 1, yi - 1, xi + 1, yi + 1, col)
        elif k == "prop":
            cv.frame(xi - 3, yi - 3, xi + 3, yi + 3, col)
        elif k == "light":
            cv.line(xi - 4, yi, xi, yi - 4, col)
            cv.line(xi, yi - 4, xi + 4, yi, col)
            cv.line(xi + 4, yi, xi, yi + 4, col)
            cv.line(xi, yi + 4, xi - 4, yi, col)
        elif k == "item":
            cv.rect(xi - 3, yi - 3, xi + 3, yi + 3, col)
        elif k in ("logic", "point"):
            cv.line(xi - 3, yi - 3, xi + 3, yi + 3, col)
            cv.line(xi - 3, yi + 3, xi + 3, yi - 3, col)
        elif k == "npc":
            arrow(x, y, e.get("angles"), 18, col, 2)
            cv.disc(xi, yi, 6, rgb(0, 0, 0))
            cv.disc(xi, yi, 5, col)
        elif k == "player":
            arrow(x, y, e.get("angles"), 28, col, 2)
            cv.disc(xi, yi, 8, rgb(0, 0, 0))
            cv.disc(xi, yi, 7, col)
        elif k == "camera":
            arrow(x, y, e.get("angles"), 28, col, 2)
            cv.ring(xi, yi, 6, col, 2)
        if hl:
            cv.ring(xi, yi, 11, HIGHLIGHT, 2)
        if args.labels == "none" and not hl:
            continue
        if hl:
            add_label(0, x, y, name or cls, HIGHLIGHT)
        elif k == "player":
            add_label(1, x, y, cls.replace("info_", ""), col)
        elif k == "camera":
            add_label(1, x, y, name or cls, col)
        elif k == "npc":
            add_label(2, x, y, name or cls, col)
        elif args.labels == "all" or (name and k not in ("node", "prop") and not many):
            add_label(5, x, y, name or cls, col)

    # overlays: leak, marks, views
    if leak:
        lp = [P3(p) for p in leak]
        cv.polyline(lp, rgb(255, 30, 30), width=3, closed=False)
        # vbsp writes the path from the void (first point) to the entity (last)
        cv.ring(int(lp[-1][0]), int(lp[-1][1]), 9, rgb(255, 30, 30), 3)
        add_label(0, lp[0][0], lp[0][1], "LEAK: void", rgb(255, 80, 80))
        add_label(0, lp[-1][0], lp[-1][1], "LEAK: entity", rgb(255, 80, 80))
    for v, label in marks:
        x, y = P3(v)
        cv.ring(int(x), int(y), 9, HIGHLIGHT, 2)
        cv.line(x - 13, y, x + 13, y, HIGHLIGHT)
        cv.line(x, y - 13, x, y + 13, HIGHLIGHT)
        if label:
            add_label(0, x, y, label, HIGHLIGHT)
    for vw in views:
        x, y = P3(vw["pos"])
        col = rgb(255, 255, 255)
        cv.ring(int(x), int(y), 6, col, 2)
        a = vw["ang"]
        fwd = forward_vector(a[0], a[1])
        du_, dv_ = fwd[ui], fwd[vi]
        ln = math.hypot(du_, dv_)
        if ln > 0.2:
            base = math.atan2(-dv_, du_)
            half = math.radians(vw.get("fov", 90) / 2.0)
            L = 40
            for da in (-half, half):
                cv.line(x, y, x + math.cos(base + da) * L, y + math.sin(base + da) * L, col)
            cv.line(x, y, x + math.cos(base) * L * 0.7, y + math.sin(base) * L * 0.7, col, 2)
        add_label(1, x, y, vw["name"], col)

    # place labels
    placed = []
    gw, gh = text_size("M", fs)
    budget = 400
    for prio, x, y, text, col in sorted(labels, key=lambda t: t[0]):
        if len(placed) >= budget and prio > 0:
            break
        text = text[:40]
        tw, th = text_size(text, fs)
        for ox, oy in ((10, -th - 4), (10, 6), (-tw - 10, -th - 4), (-tw - 10, 6)):
            rx0, ry0 = int(x + ox), int(y + oy)
            r = (rx0 - 2, ry0 - 2, rx0 + tw + 2, ry0 + th + 2)
            if r[0] < 0 or r[1] < 0 or r[2] >= W or r[3] >= H:
                continue
            if any(not (r[2] < q[0] or r[0] > q[2] or r[3] < q[1] or r[1] > q[3]) for q in placed):
                continue
            placed.append(r)
            cv.text(rx0, ry0, text, col, fs, bg=TEXT_BG)
            break

    # grid labels along the top (u) and left (v) edges
    gu = math.ceil(u0 / step) * step
    while gu <= u1:
        x = int(round((gu - u0) / s))
        t = str(int(gu))
        tw, th = text_size(t, fs)
        if 0 <= x - tw // 2 and x + tw // 2 < W:
            cv.text(x - tw // 2, H - th - 3, t, rgb(160, 170, 185), fs, bg=TEXT_BG)
        gu += step
    gv = math.ceil(v0 / step) * step
    while gv <= v1:
        y = int(round((v1 - gv) / s))
        t = str(int(gv))
        tw, th = text_size(t, fs)
        if th + 2 < y - th // 2 < H - 2 * th - 6:
            cv.text(3, y - th // 2, t, rgb(160, 170, 185), fs, bg=TEXT_BG)
        gv += step

    # title + legend
    axes_name = "xyz"
    rng = ""
    if lo != -math.inf or hi != math.inf:
        rng = "  %s %s..%s" % (axes_name[di], "-inf" if lo == -math.inf else int(lo), "inf" if hi == math.inf else int(hi))
    title = "%s  %s%s  grid %du  1px=%.2fu" % (os.path.basename(args.file), axis_desc, rng, step, s)
    tfs = fs if text_size(title, fs)[0] + 10 <= W else 1
    cv.text(4, 4, title, TEXT, tfs, bg=TEXT_BG, pad=3)
    lx, ly = 4, 4 + text_size("M", tfs)[1] + 8
    legend = [(k, KINDS[k][0]) for k in KINDS if counts.get(k)] + \
             [(k, EKINDS[k]) for k in EKINDS if ecounts.get(k)]
    for name, col in legend:
        tw, th = text_size(name, fs)
        if lx + th + 4 + tw + 10 > W:
            lx = 4
            ly += th + 6
        cv.rect(lx - 2, ly - 2, lx + th + 4 + tw + 2, ly + th + 1, TEXT_BG)
        cv.rect(lx, ly, lx + th - 1, ly + th - 1, col)
        cv.text(lx + th + 4, ly, name, TEXT, fs)
        lx += th + 4 + tw + 12

    out = args.output or (os.path.splitext(os.path.basename(args.file))[0] + "." + axis + ".png")
    cv.save_png(out)

    # text summary: everything needed to map the picture back to world coords
    un, vn = axes_name[ui], axes_name[vi]
    print(f"wrote {out}  {W}x{H}px  {axis_desc}")
    print(f"frame {un}=[{u0:.0f}, {u1:.0f}] {vn}=[{v0:.0f}, {v1:.0f}]{rng}  scale {s:.3f} units/px  grid {step}u")
    print(f"pixel->world: {un} = {u0:.1f} + px*{s:.4f} ; {vn} = {v1:.1f} - py*{s:.4f}")
    print("brushes in view: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
    print("entities in view: " + (", ".join(f"{k} {v}" for k, v in sorted(ecounts.items())) or "none"))
    if invalid:
        ids = ", ".join(str(b.id) for b in invalid[:10])
        print(f"WARN {len(invalid)} invalid brush(es) drawn in red (planes don't bound a volume): solid id {ids}")
    if unusable:
        print(f"WARN {unusable} brush(es) had no usable geometry and were skipped")
    for n in notes:
        print("note: " + n)
    if axis == "z" and lo == -math.inf and hi == math.inf and counts:
        print("tip: roofs and ceilings hide interiors in a plain top view; "
              "--cut Z (e.g. floor + 100) or --z-range ZMIN ZMAX shows one floor")


class argparse_ns:
    """Minimal stand-in for an argparse namespace (selector fields for find)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def add_parsers(sub):
    sp = sub.add_parser("render", help="draw the map as a PNG (plan view or side view) to look at it")
    sp.add_argument("file")
    sp.add_argument("-o", "--output", help="PNG path (default: <map>.<axis>.png in the current dir)")
    sp.add_argument("--axis", choices=("z", "x", "y"), default="z",
                    help="look along: z = top/plan view (default), x or y = side views")
    sp.add_argument("--slice", nargs=2, type=float, metavar=("MIN", "MAX"),
                    help="keep only brushes/entities overlapping this range on the view axis "
                         "(top view: a z range, e.g. one floor)")
    sp.add_argument("--z-range", dest="slice", nargs=2, type=float, metavar=("ZMIN", "ZMAX"),
                    help="alias of --slice for the top view")
    sp.add_argument("--cut", type=float, metavar="VALUE",
                    help="drop everything above VALUE on the view axis (top view: remove roofs/ceilings)")
    sp.add_argument("--bbox", nargs=4, type=float, metavar=("U1", "V1", "U2", "V2"),
                    help="frame in view-plane world coords (top view: x1 y1 x2 y2)")
    sp.add_argument("--size", type=int, default=1600, help="longest image side in px (default 1600)")
    sp.add_argument("--font-scale", type=int, default=2, help="label size multiplier (default 2)")
    sp.add_argument("--labels", choices=("auto", "all", "none"), default="auto")
    sp.add_argument("--class", dest="cls", help="highlight entities by classname (wildcards)")
    sp.add_argument("--targetname", help="highlight entities by targetname (wildcards)")
    sp.add_argument("--kv", help="highlight entities by KEY or KEY=VALUE")
    sp.add_argument("--focus", type=float, nargs="?", const=512.0, metavar="MARGIN",
                    help="frame the highlighted entities / overlays (+MARGIN units, default 512)")
    sp.add_argument("--leak", metavar="FILE.lin", help="draw a vbsp leak pointfile")
    sp.add_argument("--mark", action="append", metavar="'X Y Z[:LABEL]'", help="draw a marker (repeatable)")
    sp.add_argument("--views", metavar="FILE", help="draw camera views from a shoot views file")
    sp.add_argument("--tools", action="store_true", help="also draw hint/skip brushes")
    sp.add_argument("--no-nodes", action="store_true", help="hide info_node dots")
    sp.add_argument("--no-props", action="store_true", help="hide prop markers")
    sp.add_argument("--keep-skybox", action="store_true", help="don't leave the 3D skybox out of the frame")
    sp.add_argument("--editor-verts", action="store_true",
                    help="use Hammer++ vertices_plus instead of computing from planes (faster on huge maps)")
    sp.set_defaults(func=_cmd_render)
