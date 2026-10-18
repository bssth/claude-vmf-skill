#!/usr/bin/env python3
"""vmf.py — VMF (Valve Map Format / Hammer++) parser, model and serializer.

The format is recursive KeyValues:

    BlockName
    {
        "key" "value"
        ChildBlock
        {
            ...
        }
    }

Design constraints that this module is built around (see SKILL.md):
  * Keys repeat. `vertices_plus` holds many "v"; `connections` holds many
    outputs with the same name. A dict would silently collapse them, so the
    model preserves an *ordered list* of items.
  * key/value pairs and child blocks *interleave* within a block (a `side`
    has "id", "plane", then a vertices_plus block, then "material", ...), so
    items live in one ordered list, not split into "props" + "children".
  * Round-trip must be byte-exact when nothing is changed. Hammer++ writes a
    perfectly regular layout (one tab per nesting level, name and brace on
    their own lines, trailing newline), and the serializer reproduces it.
  * Line endings are detected per file (Hammer++ writes LF even in CRLF repos)
    and restored on write.

The module is both a library (parse/load/serialize/save + the Node/KV model +
brush geometry) and a CLI: `python vmf.py --help` lists the read
(stats/find/get/io-graph/refs), validate (lint/roundtrip), edit
(set-kv/add-io/rm-io/replace-material/move) and generate (new-id/box/add-entity)
commands, plus the visual loop from the sibling modules: render (vmf_render.py),
compile / shoot / game-paths (vmf_game.py).
"""

from __future__ import annotations

import sys
from typing import Iterator, List, Optional, Tuple, Union

TAB = "\t"


class KV:
    """A single "key" "value" pair. Both are raw strings (unescaped)."""

    __slots__ = ("key", "value")

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value


class Node:
    """A named block: BlockName { ...items... }.

    `items` is an ordered list whose elements are either KV or Node, preserving
    original interleaving and duplicate keys.
    """

    __slots__ = ("name", "items")

    def __init__(self, name: str, items: Optional[List[Union[KV, "Node"]]] = None):
        self.name = name
        self.items = items if items is not None else []

    # -- read helpers -------------------------------------------------------

    def kvs(self) -> Iterator[KV]:
        for it in self.items:
            if isinstance(it, KV):
                yield it

    def children(self, name: Optional[str] = None) -> Iterator["Node"]:
        for it in self.items:
            if isinstance(it, Node) and (name is None or it.name == name):
                yield it

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """First value for `key` (VMF keys may repeat; this returns the first)."""
        for it in self.items:
            if isinstance(it, KV) and it.key == key:
                return it.value
        return default

    def get_all(self, key: str) -> List[str]:
        return [it.value for it in self.items if isinstance(it, KV) and it.key == key]

    def first(self, name: str) -> Optional["Node"]:
        for c in self.children(name):
            return c
        return None

    # -- mutation helpers ---------------------------------------------------

    def set_kv(self, key: str, value: str) -> bool:
        """Set the first occurrence of `key`, or append it. Returns True if a new
        pair was added, False if an existing one was updated."""
        for it in self.items:
            if isinstance(it, KV) and it.key == key:
                it.value = value
                return False
        self.items.append(KV(key, value))
        return True

    def add_kv(self, key: str, value: str) -> None:
        self.items.append(KV(key, value))

    def remove_kvs(self, key: str, value: Optional[str] = None) -> int:
        before = len(self.items)
        self.items = [
            it for it in self.items
            if not (isinstance(it, KV) and it.key == key and (value is None or it.value == value))
        ]
        return before - len(self.items)

    def insert_child_before_editor(self, child: "Node") -> None:
        """Insert a child block, keeping it ahead of a trailing `editor` block
        (matches Hammer's usual ordering). Falls back to appending."""
        for i, it in enumerate(self.items):
            if isinstance(it, Node) and it.name == "editor":
                self.items.insert(i, child)
                return
        self.items.append(child)


class Document:
    """A whole VMF file: an ordered list of top-level Nodes plus the newline
    style used for byte-exact serialization."""

    __slots__ = ("roots", "newline", "trailing_newline")

    def __init__(self, roots: List[Node], newline: str = "\n", trailing_newline: bool = True):
        self.roots = roots
        self.newline = newline
        self.trailing_newline = trailing_newline

    def top(self, name: str) -> Optional[Node]:
        for n in self.roots:
            if n.name == name:
                return n
        return None


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class _Tok:
    STRING = "string"   # quoted "..."
    WORD = "word"       # bare identifier (block name)
    OPEN = "open"       # {
    CLOSE = "close"     # }
    EOF = "eof"


def _tokenize(text: str) -> Iterator[Tuple[str, str, int]]:
    """Yield (type, value, line) tokens. Comments (// ...) are recognised
    (Hammer never writes them) and skipped to end of line."""
    i = 0
    n = len(text)
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "{":
            yield (_Tok.OPEN, "{", line)
            i += 1
            continue
        if c == "}":
            yield (_Tok.CLOSE, "}", line)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # comment to end of line
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == '"':
            i += 1
            start = i
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\n":
                    line += 1
                buf.append(text[i])
                i += 1
            if i >= n:
                raise VMFParseError(f"unterminated string starting near line {line}")
            i += 1  # closing quote
            yield (_Tok.STRING, "".join(buf), line)
            continue
        # bare word (block name): read until whitespace or brace
        start = i
        while i < n and text[i] not in " \t\r\n{}\"":
            i += 1
        yield (_Tok.WORD, text[start:i], line)
    yield (_Tok.EOF, "", line)


class VMFParseError(Exception):
    pass


class _Parser:
    def __init__(self, text: str):
        self._toks = _tokenize(text)
        self._peeked: Optional[Tuple[str, str, int]] = None

    def _next(self) -> Tuple[str, str, int]:
        if self._peeked is not None:
            t = self._peeked
            self._peeked = None
            return t
        return next(self._toks)

    def _peek(self) -> Tuple[str, str, int]:
        if self._peeked is None:
            self._peeked = next(self._toks)
        return self._peeked

    def parse_document(self) -> List[Node]:
        roots: List[Node] = []
        while self._peek()[0] != _Tok.EOF:
            roots.append(self._parse_node())
        return roots

    def _parse_node(self) -> Node:
        ttype, tval, line = self._next()
        if ttype not in (_Tok.WORD, _Tok.STRING):
            raise VMFParseError(f"expected block name, got {ttype!r} at line {line}")
        name = tval
        otype, _, oline = self._next()
        if otype != _Tok.OPEN:
            raise VMFParseError(f"expected '{{' after block '{name}' at line {oline}")
        items: List[Union[KV, Node]] = []
        while True:
            ptype, pval, pline = self._peek()
            if ptype == _Tok.CLOSE:
                self._next()
                break
            if ptype == _Tok.EOF:
                raise VMFParseError(f"unexpected EOF inside block '{name}'")
            if ptype == _Tok.STRING:
                # could be "key" "value"  OR  a quoted block name "key" { ... }
                self._next()  # consume key
                ntype, nval, nline = self._peek()
                if ntype == _Tok.STRING:
                    self._next()
                    items.append(KV(pval, nval))
                elif ntype == _Tok.OPEN:
                    items.append(self._parse_named_block(pval))
                else:
                    raise VMFParseError(
                        f"expected value or '{{' after key '{pval}' at line {nline}")
            elif ptype == _Tok.WORD:
                items.append(self._parse_node())
            else:
                raise VMFParseError(f"unexpected token {ptype!r} at line {pline}")
        return Node(name, items)

    def _parse_named_block(self, name: str) -> Node:
        otype, _, oline = self._next()
        if otype != _Tok.OPEN:
            raise VMFParseError(f"expected '{{' after '{name}' at line {oline}")
        items: List[Union[KV, Node]] = []
        while True:
            ptype, pval, pline = self._peek()
            if ptype == _Tok.CLOSE:
                self._next()
                break
            if ptype == _Tok.EOF:
                raise VMFParseError(f"unexpected EOF inside block '{name}'")
            if ptype == _Tok.STRING:
                self._next()
                ntype, nval, nline = self._peek()
                if ntype == _Tok.STRING:
                    self._next()
                    items.append(KV(pval, nval))
                elif ntype == _Tok.OPEN:
                    items.append(self._parse_named_block(pval))
                else:
                    raise VMFParseError(
                        f"expected value or '{{' after key '{pval}' at line {nline}")
            elif ptype == _Tok.WORD:
                items.append(self._parse_node())
            else:
                raise VMFParseError(f"unexpected token {ptype!r} at line {pline}")
        return Node(name, items)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(text: str) -> Document:
    """Parse VMF source text into a Document."""
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith("\n")
    # Normalize for the tokenizer; \r is treated as whitespace anyway.
    roots = _Parser(text).parse_document()
    return Document(roots, newline=newline, trailing_newline=trailing)


def load(path: str) -> Document:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return parse(f.read())


def _serialize_node(node: Node, indent: int, out: List[str]) -> None:
    pad = TAB * indent
    out.append(pad + node.name + "\n")
    out.append(pad + "{\n")
    cpad = TAB * (indent + 1)
    for it in node.items:
        if isinstance(it, KV):
            out.append('%s"%s" "%s"\n' % (cpad, it.key, it.value))
        else:
            _serialize_node(it, indent + 1, out)
    out.append(pad + "}\n")


def serialize(doc: Document) -> str:
    """Serialize a Document back to text using canonical Hammer++ layout."""
    out: List[str] = []
    for n in doc.roots:
        _serialize_node(n, 0, out)
    text = "".join(out)
    if not doc.trailing_newline and text.endswith("\n"):
        text = text[:-1]
    if doc.newline != "\n":
        text = text.replace("\n", doc.newline)
    return text


def save(doc: Document, path: str) -> None:
    text = serialize(doc)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Traversal / selection helpers (used by the CLI)
# ---------------------------------------------------------------------------

def iter_all(node: Node):
    """Depth-first yield of every Node in a subtree (including `node`)."""
    yield node
    for it in node.items:
        if isinstance(it, Node):
            yield from iter_all(it)


def iter_entities(doc: Document):
    for n in doc.roots:
        if n.name == "entity":
            yield n


def find_by_id(doc: Document, target_id: str) -> Optional[Node]:
    for root in doc.roots:
        for n in iter_all(root):
            if n.get("id") == target_id:
                return n
    return None


def _glob_match(pattern: str, value: str) -> bool:
    """Source-style wildcard match. '*' matches any run of chars. Used both for
    CLI selectors and for I/O targetname wildcards."""
    import fnmatch
    if "*" not in pattern and "?" not in pattern:
        return pattern == value
    return fnmatch.fnmatchcase(value, pattern)


def is_brush_entity(ent: Node) -> bool:
    return ent.first("solid") is not None


# Targets that never need a matching targetname.
_SPECIAL_TARGETS = {
    "!self", "!player", "!activator", "!caller", "!pvsplayer", "!speechtarget",
    "!target1", "!target2", "!target3", "!target4", "", "!firstplayer",
}


def parse_connection(value: str):
    """Split an I/O value 'target,input,param,delay,times' (Hammer++ uses comma;
    older VMFs used \\x1b). Returns a dict; missing fields are ''."""
    sep = "\x1b" if "\x1b" in value else ","
    parts = value.split(sep)
    parts += [""] * (5 - len(parts))
    return {
        "target": parts[0],
        "input": parts[1],
        "param": parts[2],
        "delay": parts[3],
        "times": parts[4],
    }


def max_id(doc: Document) -> int:
    m = 0
    for root in doc.roots:
        for n in iter_all(root):
            v = n.get("id")
            if v is not None and v.isdigit():
                m = max(m, int(v))
    return m


class IdAllocator:
    def __init__(self, start: int):
        self._next = start + 1

    def take(self) -> str:
        v = self._next
        self._next += 1
        return str(v)


def make_box(mins, maxs, material: str, alloc: IdAllocator,
             lightmapscale: str = "16", color: str = "0 180 137") -> Node:
    """Build an axis-aligned box `solid` with 6 sides whose plane normals point
    outward (what vbsp needs), correct uaxis/vaxis per face, fresh ids and an
    editor block. mins/maxs are (x,y,z) tuples of numbers."""
    x0, y0, z0 = (min(mins[i], maxs[i]) for i in range(3))
    x1, y1, z1 = (max(mins[i], maxs[i]) for i in range(3))

    # corners
    def C(x, y, z):
        return (x, y, z)

    # (4 corners, outward-axis, uaxis-vector, vaxis-vector) per face
    faces = [
        # +Z top
        ([C(x0, y1, z1), C(x1, y1, z1), C(x1, y0, z1), C(x0, y0, z1)], (0, 0, 1), (1, 0, 0), (0, -1, 0)),
        # -Z bottom
        ([C(x0, y0, z0), C(x1, y0, z0), C(x1, y1, z0), C(x0, y1, z0)], (0, 0, -1), (1, 0, 0), (0, -1, 0)),
        # -Y front
        ([C(x0, y0, z1), C(x1, y0, z1), C(x1, y0, z0), C(x0, y0, z0)], (0, -1, 0), (1, 0, 0), (0, 0, -1)),
        # +Y back
        ([C(x1, y1, z1), C(x0, y1, z1), C(x0, y1, z0), C(x1, y1, z0)], (0, 1, 0), (1, 0, 0), (0, 0, -1)),
        # -X left
        ([C(x0, y1, z1), C(x0, y0, z1), C(x0, y0, z0), C(x0, y1, z0)], (-1, 0, 0), (0, 1, 0), (0, 0, -1)),
        # +X right
        ([C(x1, y0, z1), C(x1, y1, z1), C(x1, y1, z0), C(x1, y0, z0)], (1, 0, 0), (0, 1, 0), (0, 0, -1)),
    ]

    def sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    def fmt_num(n):
        # integers print without trailing .0 to match Hammer's typical output
        if isinstance(n, float) and n.is_integer():
            n = int(n)
        return str(n)

    def fmt_pt(p):
        return "(%s %s %s)" % (fmt_num(p[0]), fmt_num(p[1]), fmt_num(p[2]))

    solid = Node("solid", [KV("id", alloc.take())])
    for corners, out_axis, uvec, vvec in faces:
        pts = list(corners)
        # vbsp's outward normal is (p0-p1) x (p2-p1), i.e. (p1-p0) x (p2-p0)
        # must point INTO the brush (see plane_from_points).
        n = cross(sub(pts[1], pts[0]), sub(pts[2], pts[0]))
        if dot(n, out_axis) > 0:
            pts = pts[::-1]
        side = Node("side", [])
        side.add_kv("id", alloc.take())
        side.add_kv("plane", "%s %s %s" % (fmt_pt(pts[0]), fmt_pt(pts[1]), fmt_pt(pts[2])))
        vp = Node("vertices_plus", [])
        for c in corners:
            vp.add_kv("v", "%s %s %s" % (fmt_num(c[0]), fmt_num(c[1]), fmt_num(c[2])))
        side.items.append(vp)
        side.add_kv("material", material)
        side.add_kv("uaxis", "[%d %d %d 0] 0.25" % uvec)
        side.add_kv("vaxis", "[%d %d %d 0] 0.25" % vvec)
        side.add_kv("rotation", "0")
        side.add_kv("lightmapscale", lightmapscale)
        side.add_kv("smoothing_groups", "0")
        solid.items.append(side)
    ed = Node("editor", [KV("color", color), KV("visgroupshown", "1"), KV("visgroupautoshown", "1")])
    solid.items.append(ed)
    return solid


# ---------------------------------------------------------------------------
# Brush geometry (planes -> face polygons), shared by lint and render
# ---------------------------------------------------------------------------

def parse_plane_points(value: str):
    """'(x y z) (x y z) (x y z)' -> three (x, y, z) float tuples. Raises ValueError."""
    import re
    pts = [tuple(float(v) for v in g.split()) for g in re.findall(r"\(([^)]*)\)", value)]
    if len(pts) != 3 or any(len(p) != 3 for p in pts):
        raise ValueError("bad plane: %r" % value)
    return pts


def plane_from_points(p0, p1, p2):
    """(normal, dist) exactly as vbsp's PlaneFromPoints builds it: the normal is
    (p0-p1) x (p2-p1) and points OUT of the brush, so the brush is the region
    where dot(normal, x) <= dist. None for a degenerate (zero-area) plane."""
    t1 = (p0[0] - p1[0], p0[1] - p1[1], p0[2] - p1[2])
    t2 = (p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2])
    n = (t1[1] * t2[2] - t1[2] * t2[1], t1[2] * t2[0] - t1[0] * t2[2], t1[0] * t2[1] - t1[1] * t2[0])
    ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5
    if ln < 1e-9:
        return None
    n = (n[0] / ln, n[1] / ln, n[2] / ln)
    return n, n[0] * p0[0] + n[1] * p0[1] + n[2] * p0[2]


def _base_winding(n, d, size=131072.0):
    """A huge quad lying on the plane (Quake-style BaseWindingForPlane)."""
    axis = max(range(3), key=lambda i: abs(n[i]))
    up = (1.0, 0.0, 0.0) if axis == 2 else (0.0, 0.0, 1.0)
    v = up[0] * n[0] + up[1] * n[1] + up[2] * n[2]
    up = (up[0] - v * n[0], up[1] - v * n[1], up[2] - v * n[2])
    ln = (up[0] * up[0] + up[1] * up[1] + up[2] * up[2]) ** 0.5
    up = (up[0] / ln * size, up[1] / ln * size, up[2] / ln * size)
    r = (up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0])
    o = (n[0] * d, n[1] * d, n[2] * d)
    return [
        (o[0] - r[0] + up[0], o[1] - r[1] + up[1], o[2] - r[2] + up[2]),
        (o[0] + r[0] + up[0], o[1] + r[1] + up[1], o[2] + r[2] + up[2]),
        (o[0] + r[0] - up[0], o[1] + r[1] - up[1], o[2] + r[2] - up[2]),
        (o[0] - r[0] - up[0], o[1] - r[1] - up[1], o[2] - r[2] - up[2]),
    ]


def _clip_back(w, n, d, eps=0.01):
    """Keep the part of polygon `w` behind the plane (dot(n, x) <= d)."""
    dists = [p[0] * n[0] + p[1] * n[1] + p[2] * n[2] - d for p in w]
    if all(x <= eps for x in dists):
        return w
    if all(x >= -eps for x in dists):
        return []
    out = []
    m = len(w)
    for i in range(m):
        p, dp = w[i], dists[i]
        q, dq = w[(i + 1) % m], dists[(i + 1) % m]
        if dp <= eps:
            out.append(p)
        if (dp < -eps and dq > eps) or (dp > eps and dq < -eps):
            t = dp / (dp - dq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]), p[2] + t * (q[2] - p[2])))
    return out


def solid_windings(solid: Node):
    """Face polygons of a brush computed from its side planes the way vbsp sees
    them (vertices_plus is ignored). Returns [(side_node, [points])] for the
    faces that survive clipping; a valid convex brush has >= 4 of them, an
    inverted (normals pointing inward) or open brush has fewer."""
    planes = []
    for side in solid.children("side"):
        pv = side.get("plane")
        try:
            pl = plane_from_points(*parse_plane_points(pv)) if pv else None
        except ValueError:
            pl = None
        planes.append((side, pl))
    out = []
    for i, (side, pl) in enumerate(planes):
        if pl is None:
            continue
        w = _base_winding(*pl)
        for j, (_other_side, other) in enumerate(planes):
            if j != i and other is not None:
                w = _clip_back(w, *other)
                if not w:
                    break
        if len(w) >= 3:
            out.append((side, w))
    return out


def solid_vertices(solid: Node, prefer_editor_verts: bool = False):
    """Corner points of a brush, computed from the planes (what compiles). With
    prefer_editor_verts, Hammer++'s vertices_plus are used when every side has
    them (faster, but they are an editor hint that can drift from the planes).
    Returns (points, valid): valid is False when the planes don't bound a
    volume; the points then fall back to vertices_plus if there are any."""
    def editor_verts():
        pts = []
        for side in solid.children("side"):
            vp = side.first("vertices_plus")
            if vp is None:
                return []
            for v in vp.get_all("v"):
                try:
                    x, y, z = (float(c) for c in v.split())
                except ValueError:
                    return []
                pts.append((x, y, z))
        return pts

    if prefer_editor_verts:
        pts = editor_verts()
        if pts:
            return pts, True
    faces = solid_windings(solid)
    if len(faces) >= 4:
        return [p for _side, w in faces for p in w], True
    return editor_verts(), False


def _write_or_preview(doc: Document, path: str, src_before: str, dry_run: bool, changed_desc: str) -> None:
    """Serialize, sanity-check it re-parses, then either write in place or show a
    bounded unified diff. Always re-parses the output as a guard against producing
    invalid VMF."""
    out = serialize(doc)
    # guard: output must parse back cleanly
    try:
        parse(out)
    except VMFParseError as e:
        print(f"ABORT: produced invalid VMF ({e}); nothing written", file=sys.stderr)
        sys.exit(3)
    if out == src_before:
        print("no change")
        return
    if dry_run:
        import difflib
        diff = difflib.unified_diff(
            src_before.splitlines(keepends=True), out.splitlines(keepends=True),
            fromfile=path + " (before)", tofile=path + " (after)", n=2)
        shown = 0
        for ln in diff:
            sys.stdout.write(ln)
            shown += 1
            if shown > 200:
                sys.stdout.write("... (diff truncated)\n")
                break
        print(f"\n[dry-run] {changed_desc}; not written")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    print(f"{changed_desc}; written to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ent_summary(ent: Node) -> dict:
    return {
        "id": ent.get("id"),
        "classname": ent.get("classname"),
        "targetname": ent.get("targetname"),
        "origin": ent.get("origin"),
        "brush": is_brush_entity(ent),
    }


def _cmd_stats(args):
    import json as _json
    from collections import Counter
    doc = load(args.file)
    world = doc.top("world")
    world_solids = len(list(world.children("solid"))) if world else 0
    classes = Counter()
    brush_ents = 0
    targetnames = []
    total_sides = 0
    if world:
        for s in world.children("solid"):
            total_sides += len(list(s.children("side")))
    for ent in iter_entities(doc):
        classes[ent.get("classname") or "<none>"] += 1
        if is_brush_entity(ent):
            brush_ents += 1
            for s in ent.children("solid"):
                total_sides += len(list(s.children("side")))
        tn = ent.get("targetname")
        if tn:
            targetnames.append(tn)
    data = {
        "file": args.file,
        "world_solids": world_solids,
        "entities": sum(classes.values()),
        "brush_entities": brush_ents,
        "point_entities": sum(classes.values()) - brush_ents,
        "total_brush_sides": total_sides,
        "unique_targetnames": len(set(targetnames)),
        "classes": dict(classes.most_common()),
    }
    if args.json:
        print(_json.dumps(data, indent=2))
        return
    print(f"file:              {data['file']}")
    print(f"world solids:      {data['world_solids']}")
    print(f"entities:          {data['entities']}  (brush {brush_ents}, point {data['point_entities']})")
    print(f"brush sides total: {data['total_brush_sides']}")
    print(f"unique targetnames:{data['unique_targetnames']}")
    print(f"\nclassname counts ({len(classes)} distinct):")
    for cn, c in classes.most_common():
        print(f"  {c:>6}  {cn}")


def _entity_matches(ent: Node, args) -> bool:
    if args.cls is not None and not _glob_match(args.cls, ent.get("classname") or ""):
        return False
    if args.targetname is not None and not _glob_match(args.targetname, ent.get("targetname") or ""):
        return False
    if args.kv is not None:
        key, _, val = args.kv.partition("=")
        if "=" not in args.kv:
            if ent.get(key) is None:
                return False
        else:
            got = ent.get(key)
            if got is None or not _glob_match(val, got):
                return False
    return True


def _cmd_find(args):
    import json as _json
    doc = load(args.file)
    matches = [e for e in iter_entities(doc) if _entity_matches(e, args)]
    total = len(matches)
    shown = matches[: args.limit] if args.limit else matches
    if args.json:
        print(_json.dumps({"total": total, "shown": len(shown),
                           "matches": [_ent_summary(e) for e in shown]}, indent=2))
        return
    print(f"{total} match(es)" + (f", showing {len(shown)}" if len(shown) < total else ""))
    for e in shown:
        s = _ent_summary(e)
        extra = f" @{s['origin']}" if s["origin"] else (" [brush]" if s["brush"] else "")
        tn = f"  '{s['targetname']}'" if s["targetname"] else ""
        print(f"  id={s['id']:<8} {s['classname']}{tn}{extra}")
    if args.limit and total > args.limit:
        print(f"  ... {total - args.limit} more (raise --limit)")


def _cmd_get(args):
    doc = load(args.file)
    if args.id:
        node = find_by_id(doc, args.id)
        if node is None:
            print(f"no node with id {args.id}", file=sys.stderr)
            sys.exit(1)
        out: List[str] = []
        _serialize_node(node, 0, out)
        sys.stdout.write("".join(out))
        return
    # by targetname: dump all matching entities
    found = 0
    for e in iter_entities(doc):
        if _glob_match(args.targetname, e.get("targetname") or ""):
            out = []
            _serialize_node(e, 0, out)
            sys.stdout.write("".join(out))
            found += 1
    if not found:
        print(f"no entity with targetname '{args.targetname}'", file=sys.stderr)
        sys.exit(1)


def _cmd_io(args):
    doc = load(args.file)
    # Build name->entity and gather edges.
    edges = []  # (src_targetname, output, conn)
    for e in iter_entities(doc):
        conns = e.first("connections")
        if not conns:
            continue
        src = e.get("targetname") or f"<id {e.get('id')} {e.get('classname')}>"
        for kv in conns.kvs():
            edges.append((src, kv.key, parse_connection(kv.value)))
    if args.targetname:
        pat = args.targetname
        edges = [(s, o, c) for (s, o, c) in edges
                 if _glob_match(pat, s) or _glob_match(pat, c["target"])]
    print(f"{len(edges)} connection(s)")
    for src, out, c in edges:
        print(f"  {src}  --{out}-->  {c['target']}.{c['input']}"
              + (f"({c['param']})" if c["param"] else "")
              + (f"  delay={c['delay']}" if c["delay"] not in ("", "0") else "")
              + (f"  times={c['times']}" if c["times"] not in ("", "-1") else ""))


def _cmd_refs(args):
    import json as _json
    doc = load(args.file)
    name = args.name

    def name_match(stored: str) -> bool:
        if stored == name:
            return True
        if any(c in stored for c in "*?") and _glob_match(stored, name):
            return True
        if any(c in name for c in "*?") and _glob_match(name, stored):
            return True
        return False

    defs = []   # entities whose targetname is the queried name (definition sites)
    refs = []   # (entity, how-string)
    for e in iter_entities(doc):
        tn = e.get("targetname")
        if tn is not None and name_match(tn):
            defs.append(e)
        # keyvalue references (the entity's own targetname is a definition, skip it)
        for kv in e.kvs():
            if kv.key == "targetname":
                continue
            # parentname can carry ",attachment"; match the leading name too
            if name_match(kv.value) or name_match(kv.value.split(",")[0]):
                refs.append((e, f'{kv.key}="{kv.value}"'))
        # I/O references
        conns = e.first("connections")
        if conns:
            for kv in conns.kvs():
                c = parse_connection(kv.value)
                if name_match(c["target"]):
                    refs.append((e, f'{kv.key} -> {c["target"]}.{c["input"]}'))

    def line(e):
        tn = e.get("targetname")
        return f"id={e.get('id')} {e.get('classname')}" + (f" '{tn}'" if tn else "")

    if args.json:
        print(_json.dumps({
            "name": name,
            "defined_by": [_ent_summary(e) for e in defs],
            "referenced_by": [{**_ent_summary(e), "via": how} for e, how in refs],
        }, indent=2))
        return
    print(f"references to '{name}': {len(defs)} definition(s), {len(refs)} reference(s)")
    if defs:
        print("  defined by:")
        for e in defs:
            print(f"    {line(e)}")
    if refs:
        print("  referenced by:")
        for e, how in refs[: args.limit]:
            print(f"    {line(e):<48} via  {how}")
        if len(refs) > args.limit:
            print(f"    ... {len(refs) - args.limit} more (raise --limit)")


def _cmd_lint(args):
    doc = load(args.file)
    problems = {"dup_ids": [], "dangling_io": [], "degenerate_planes": []}

    # 1) duplicate ids. Soft check: solids/sides/entities share one id space and
    # collisions do occur after copy-paste / prefab / vmf-merge. Hammer reassigns
    # them on load, so this is informational, not a compile blocker. We report the
    # node kinds involved so it's actionable.
    from collections import defaultdict
    id_kinds = defaultdict(list)
    for root in doc.roots:
        for n in iter_all(root):
            v = n.get("id")
            if v is not None:
                id_kinds[v].append(n.name)
    dup = [(i, kinds) for i, kinds in id_kinds.items() if len(kinds) > 1]
    dup.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    problems["dup_ids"] = [f"id={i}: {', '.join(sorted(set(k)))} (x{len(k)})" for i, k in dup]

    # 2) dangling I/O targets
    targetnames = set()
    for e in iter_entities(doc):
        tn = e.get("targetname")
        if tn:
            targetnames.add(tn)
    seen = set()
    for e in iter_entities(doc):
        conns = e.first("connections")
        if not conns:
            continue
        src = e.get("targetname") or f"id {e.get('id')}"
        for kv in conns.kvs():
            tgt = parse_connection(kv.value)["target"]
            if tgt in _SPECIAL_TARGETS or tgt.startswith("!"):
                continue
            if "*" in tgt or "?" in tgt:
                if any(_glob_match(tgt, tn) for tn in targetnames):
                    continue
            elif tgt in targetnames:
                continue
            key = (src, kv.key, tgt)
            if key not in seen:
                seen.add(key)
                problems["dangling_io"].append(f"{src} --{kv.key}--> '{tgt}' (no such targetname)")

    # 3) degenerate planes (zero-area: 3 collinear/coincident points)
    for root in doc.roots:
        for n in iter_all(root):
            if n.name == "side":
                pv = n.get("plane")
                if pv:
                    try:
                        if plane_from_points(*parse_plane_points(pv)) is None:
                            problems["degenerate_planes"].append(f"side id={n.get('id')} plane={pv}")
                    except ValueError:
                        problems["degenerate_planes"].append(f"side id={n.get('id')} unparseable plane={pv}")

    # 4) brushes whose planes don't bound a convex volume: inverted (normals
    # pointing inward, e.g. a hand-written plane with the wrong winding) or open.
    # vbsp drops or mangles these silently.
    problems["invalid_brushes"] = []
    for root in doc.roots:
        for n in iter_all(root):
            if n.name == "solid":
                nsides = sum(1 for _ in n.children("side"))
                nfaces = len(solid_windings(n))
                if nfaces < 4:
                    problems["invalid_brushes"].append(
                        f"solid id={n.get('id')}: only {nfaces} of {nsides} planes bound a volume "
                        f"(inverted winding or open brush)")

    # dup_ids is informational (Hammer reassigns on load); the others are real.
    severity = {"dup_ids": "INFO", "dangling_io": "WARN", "degenerate_planes": "WARN",
                "invalid_brushes": "WARN"}
    hard = 0
    for cat, items in problems.items():
        sev = severity.get(cat, "WARN")
        if not items:
            print(f"OK   {cat}: none")
            continue
        if sev != "INFO":
            hard += len(items)
        print(f"{sev} {cat}: {len(items)}")
        for it in items[: args.limit]:
            print(f"       {it}")
        if len(items) > args.limit:
            print(f"       ... {len(items) - args.limit} more")
    sys.exit(1 if hard else 0)


def _cmd_roundtrip(args):
    with open(args.file, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    out = serialize(parse(src))
    if out == src:
        print(f"OK  {args.file}  ({len(src)} bytes)")
    else:
        print(f"DIFF {args.file}: lens {len(src)} -> {len(out)}")
        sys.exit(1)


def _fmt_coord(n: float) -> str:
    if n == int(n):
        return str(int(n))
    return ("%f" % n).rstrip("0").rstrip(".")


def _select_targets(doc: Document, args) -> List[Node]:
    if getattr(args, "id", None):
        n = find_by_id(doc, args.id)
        return [n] if n is not None else []
    return [e for e in iter_entities(doc) if _entity_matches(e, args)]


def _require_targets(targets, args):
    if not targets:
        if getattr(args, "id", None):
            print(f"no node with id {args.id}", file=sys.stderr)
        else:
            print("selector matched no entities (use --class/--targetname/--kv/--id)", file=sys.stderr)
        sys.exit(1)


def _cmd_setkv(args):
    doc = load(args.file)
    src = serialize(doc)
    targets = _select_targets(doc, args)
    _require_targets(targets, args)
    for t in targets:
        t.set_kv(args.key, args.value)
    _write_or_preview(doc, args.file, src, args.dry_run,
                      f'set "{args.key}" "{args.value}" on {len(targets)} node(s)')


def _cmd_addio(args):
    doc = load(args.file)
    src = serialize(doc)
    targets = _select_targets(doc, args)
    _require_targets(targets, args)
    value = ",".join([args.target, args.input, args.param, args.delay, args.times])
    for t in targets:
        conns = t.first("connections")
        if conns is None:
            conns = Node("connections", [])
            t.insert_child_before_editor(conns)
        conns.add_kv(args.output, value)
    _write_or_preview(doc, args.file, src, args.dry_run,
                      f'added I/O {args.output} -> {args.target}.{args.input} on {len(targets)} entity(ies)')


def _cmd_rmio(args):
    doc = load(args.file)
    src = serialize(doc)
    targets = _select_targets(doc, args)
    _require_targets(targets, args)
    removed = 0
    for t in targets:
        conns = t.first("connections")
        if conns is None:
            continue
        keep = []
        for it in conns.items:
            if isinstance(it, KV) and it.key == args.output:
                c = parse_connection(it.value)
                if args.target and not _glob_match(args.target, c["target"]):
                    keep.append(it)
                    continue
                if args.input and not _glob_match(args.input, c["input"]):
                    keep.append(it)
                    continue
                removed += 1
                continue
            keep.append(it)
        conns.items = keep
        # drop now-empty connections block
        if not any(isinstance(i, KV) for i in conns.items):
            t.items = [i for i in t.items if i is not conns]
    _write_or_preview(doc, args.file, src, args.dry_run, f"removed {removed} I/O connection(s)")


def _cmd_replace_material(args):
    doc = load(args.file)
    src = serialize(doc)
    count = 0
    for root in doc.roots:
        for n in iter_all(root):
            if n.name == "side":
                for kv in n.items:
                    if isinstance(kv, KV) and kv.key == "material":
                        if _glob_match(args.old, kv.value):
                            kv.value = args.new
                            count += 1
    _write_or_preview(doc, args.file, src, args.dry_run,
                      f"replaced material on {count} face(s): {args.old} -> {args.new}")


def _translate_solid(solid: Node, d) -> None:
    import re
    for side in solid.children("side"):
        for kv in side.items:
            if isinstance(kv, KV) and kv.key == "plane":
                def repl(m):
                    x, y, z = (float(v) for v in m.group(1).split())
                    return "(%s %s %s)" % (_fmt_coord(x + d[0]), _fmt_coord(y + d[1]), _fmt_coord(z + d[2]))
                kv.value = re.sub(r"\(([^)]*)\)", repl, kv.value)
        vp = side.first("vertices_plus")
        if vp:
            for kv in vp.items:
                if isinstance(kv, KV) and kv.key == "v":
                    x, y, z = (float(v) for v in kv.value.split())
                    kv.value = "%s %s %s" % (_fmt_coord(x + d[0]), _fmt_coord(y + d[1]), _fmt_coord(z + d[2]))


def _cmd_move(args):
    doc = load(args.file)
    src = serialize(doc)
    targets = _select_targets(doc, args)
    _require_targets(targets, args)
    d = (args.dx, args.dy, args.dz)
    moved = 0
    for t in targets:
        origin = t.get("origin")
        if origin is not None:
            try:
                x, y, z = (float(v) for v in origin.split())
                t.set_kv("origin", "%s %s %s" % (_fmt_coord(x + d[0]), _fmt_coord(y + d[1]), _fmt_coord(z + d[2])))
                moved += 1
            except ValueError:
                pass
        # translate any brush geometry (the node itself if a solid, or child solids)
        if t.name == "solid":
            _translate_solid(t, d)
            moved += 1
        for s in t.children("solid"):
            _translate_solid(s, d)
            moved += 1
    _write_or_preview(doc, args.file, src, args.dry_run,
                      f"moved {moved} object(s) by ({args.dx}, {args.dy}, {args.dz}) "
                      f"[texture lock NOT applied]")


def _cmd_newid(args):
    doc = load(args.file)
    alloc = IdAllocator(max_id(doc))
    print(" ".join(alloc.take() for _ in range(args.count)))


def _cmd_box(args):
    doc = load(args.file)
    alloc = IdAllocator(max_id(doc))
    solid = make_box(tuple(args.mins), tuple(args.maxs), args.material, alloc)
    if args.into_world:
        src = serialize(doc)
        world = doc.top("world")
        if world is None:
            print("no world block", file=sys.stderr)
            sys.exit(1)
        world.items.append(solid)
        _write_or_preview(doc, args.file, src, args.dry_run, "inserted box into world")
    else:
        out: List[str] = []
        _serialize_node(solid, 1, out)  # indent 1 = ready to paste inside world
        sys.stdout.write("".join(out))


def _cmd_add_entity(args):
    doc = load(args.file)
    src = serialize(doc)
    alloc = IdAllocator(max_id(doc))
    ent = Node("entity", [KV("id", alloc.take()), KV("classname", args.cls)])
    if args.targetname:
        ent.add_kv("targetname", args.targetname)
    for pair in args.kv or []:
        k, _, v = pair.partition("=")
        ent.add_kv(k, v)
    if args.origin:
        ent.add_kv("origin", args.origin)
    ent.items.append(Node("editor", [KV("color", "220 30 220"),
                                      KV("visgroupshown", "1"), KV("visgroupautoshown", "1")]))
    # insert before trailing cameras/cordon blocks
    idx = len(doc.roots)
    for i, r in enumerate(doc.roots):
        if r.name in ("cameras", "cordon", "cordons"):
            idx = i
            break
    doc.roots.insert(idx, ent)
    _write_or_preview(doc, args.file, src, args.dry_run,
                      f"added {args.cls} entity id={ent.get('id')}")


def _add_selector_args(sp):
    sp.add_argument("--class", dest="cls", help="match classname (wildcards * ?)")
    sp.add_argument("--targetname", help="match targetname (wildcards * ?)")
    sp.add_argument("--kv", help="match KEY or KEY=VALUE")
    sp.add_argument("--id", help="match a single node by id (any solid/side/entity)")
    sp.add_argument("--dry-run", action="store_true", help="show a diff, write nothing")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="vmf.py", description="VMF (Hammer++) inspector/editor")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("stats", help="map overview: counts by classname, brushes, targetnames")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_stats)

    sp = sub.add_parser("find", help="locate entities by selector (compact summary, no full bodies)")
    sp.add_argument("file")
    sp.add_argument("--class", dest="cls", help="classname (supports * ? wildcards)")
    sp.add_argument("--targetname", help="targetname (supports * ? wildcards)")
    sp.add_argument("--kv", help="KEY or KEY=VALUE (VALUE supports wildcards)")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_find)

    sp = sub.add_parser("get", help="dump ONE subtree by id, or all entities by targetname (the surgical slice)")
    sp.add_argument("file")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--id")
    g.add_argument("--targetname")
    sp.set_defaults(func=_cmd_get)

    sp = sub.add_parser("io-graph", help="list I/O connections (filter with --targetname)")
    sp.add_argument("file")
    sp.add_argument("--targetname", help="only edges touching this name (wildcards ok)")
    sp.set_defaults(func=_cmd_io)

    sp = sub.add_parser("refs", help="reverse lookup: every entity referencing a targetname (keyvalues + I/O)")
    sp.add_argument("file")
    sp.add_argument("name", help="targetname to find references to (wildcards * ? ok)")
    sp.add_argument("--limit", type=int, default=80)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_refs)

    sp = sub.add_parser("lint", help="validate: duplicate ids, dangling I/O, degenerate planes")
    sp.add_argument("file")
    sp.add_argument("--limit", type=int, default=20, help="max examples per category")
    sp.set_defaults(func=_cmd_lint)

    sp = sub.add_parser("set-kv", help="set a keyvalue on selected nodes (entity selector or --id)")
    sp.add_argument("file")
    sp.add_argument("key")
    sp.add_argument("value")
    _add_selector_args(sp)
    sp.set_defaults(func=_cmd_setkv)

    sp = sub.add_parser("add-io", help="add an I/O connection (output) to selected entities")
    sp.add_argument("file")
    sp.add_argument("--output", required=True, help="output name, e.g. OnTrigger")
    sp.add_argument("--target", required=True, help="target entity targetname")
    sp.add_argument("--input", required=True, help="input name, e.g. Enable")
    sp.add_argument("--param", default="", help="parameter override")
    sp.add_argument("--delay", default="0")
    sp.add_argument("--times", default="-1", help="times to fire (-1 = unlimited)")
    _add_selector_args(sp)
    sp.set_defaults(func=_cmd_addio)

    sp = sub.add_parser("rm-io", help="remove I/O connections by output (optionally filtered)")
    sp.add_argument("file")
    sp.add_argument("--output", required=True)
    sp.add_argument("--target", help="only remove edges to this target (wildcards ok)")
    sp.add_argument("--input", help="only remove edges with this input (wildcards ok)")
    _add_selector_args(sp)
    sp.set_defaults(func=_cmd_rmio)

    sp = sub.add_parser("replace-material", help="swap a material across all faces (wildcards ok)")
    sp.add_argument("file")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_replace_material)

    sp = sub.add_parser("move", help="translate selected entities/brushes by (dx dy dz); no texture lock")
    sp.add_argument("file")
    sp.add_argument("dx", type=float)
    sp.add_argument("dy", type=float)
    sp.add_argument("dz", type=float)
    _add_selector_args(sp)
    sp.set_defaults(func=_cmd_move)

    sp = sub.add_parser("new-id", help="allocate fresh unique ids (max+1...)")
    sp.add_argument("file")
    sp.add_argument("--count", type=int, default=1)
    sp.set_defaults(func=_cmd_newid)

    sp = sub.add_parser("box", help="generate an axis-aligned box solid with valid planes/ids")
    sp.add_argument("file", help="source map (used to allocate non-colliding ids)")
    sp.add_argument("--mins", nargs=3, type=float, required=True, metavar=("X", "Y", "Z"))
    sp.add_argument("--maxs", nargs=3, type=float, required=True, metavar=("X", "Y", "Z"))
    sp.add_argument("--material", default="DEV/DEV_MEASUREGENERIC01")
    sp.add_argument("--into-world", action="store_true", help="insert into the map's world and write")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_box)

    sp = sub.add_parser("add-entity", help="append a point entity with a fresh id")
    sp.add_argument("file")
    sp.add_argument("--class", dest="cls", required=True)
    sp.add_argument("--origin", help='"x y z"')
    sp.add_argument("--targetname")
    sp.add_argument("--kv", action="append", help="KEY=VALUE (repeatable)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_add_entity)

    sp = sub.add_parser("roundtrip", help="assert parse->serialize is byte-exact")
    sp.add_argument("file")
    sp.set_defaults(func=_cmd_roundtrip)

    # render / compile / shoot live in sibling modules. Hand them this module
    # instead of letting `import vmf` load a second copy of it as __main__.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    sys.modules.setdefault("vmf", sys.modules[__name__])
    import vmf_game
    import vmf_render
    vmf_render.add_parsers(sub)
    vmf_game.add_parsers(sub)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
