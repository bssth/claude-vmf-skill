#!/usr/bin/env python3
"""vmf_assets.py - what the game can load: the files under the mod's
gameinfo.txt search paths (loose folders and VPK archives).

  assets    list files matching a pattern (materials/dev/*.vmt,
            models/props_c17/*bench*), or with --missing MAP.vmf the
            materials, models and skybox a map uses that the game can't find

Only the `game` search paths count (the ones the engine loads content from).
`|gameinfo_path|` and `|all_source_engine_paths|` are expanded, `*` in a path
is a folder wildcard, and `foo.vpk` means the archive `foo_dir.vpk`. VPK
directories are read straight from the archive header (versions 1 and 2), so
nothing has to be extracted.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import struct
import sys

import vmf


# ---------------------------------------------------------------------------
# Search paths
# ---------------------------------------------------------------------------

def _strip_comment(line):
    out, quoted = [], False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", i):
            break
        out.append(c)
        i += 1
    return "".join(out)


def search_paths(game_dir, exe_dir=None):
    """[(kind, path)] in priority order: kind 'vpk' (a *_dir.vpk file) or 'dir'."""
    game_dir = os.path.abspath(game_dir)
    try:
        with open(os.path.join(game_dir, "gameinfo.txt"), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return [("dir", game_dir)]
    m = re.search(r"SearchPaths\s*\{", text, re.I)
    if not m:
        return [("dir", game_dir)]
    depth, i = 1, m.end()
    start = i
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    body = text[start:i - 1]
    root = exe_dir or os.path.dirname(game_dir)
    out = []
    for raw in body.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        parts = [p.strip('"') for p in re.findall(r'"[^"]*"|\S+', line)]
        if len(parts) < 2:
            continue
        key, val = parts[0].lower(), parts[1]
        if "game" not in key.split("+"):
            continue
        val = re.sub(r"\|gameinfo_path\|", game_dir.replace("\\", "/") + "/", val, flags=re.I)
        val = re.sub(r"\|all_source_engine_paths\|", root.replace("\\", "/") + "/", val, flags=re.I)
        if not os.path.isabs(val):
            val = os.path.join(root, val)
        val = os.path.normpath(val)
        if val.lower().endswith(".vpk"):
            d = val[:-4] + "_dir.vpk"
            if os.path.isfile(d):
                out.append(("vpk", d))
            elif os.path.isfile(val):
                out.append(("vpk", val))
            continue
        for p in (sorted(glob.glob(val)) if "*" in val else [val]):
            if os.path.isdir(p):
                out.append(("dir", p))
    if not any(k == "dir" and os.path.normcase(p) == os.path.normcase(game_dir) for k, p in out):
        out.insert(0, ("dir", game_dir))
    return out


# ---------------------------------------------------------------------------
# VPK directories
# ---------------------------------------------------------------------------

def vpk_files(path):
    """Every file path stored in a VPK directory, lowercase with '/'."""
    return list(vpk_entries(path))


def vpk_entries(path):
    """{relpath: (archive_index, offset, length, preload_bytes)} of a VPK
    directory. archive_index 0x7fff = the data follows the tree in this file."""
    with open(path, "rb") as f:
        data = f.read()
    sig, ver, tree = struct.unpack_from("<III", data, 0)
    if sig != 0x55AA1234:
        raise ValueError(f"{path}: not a VPK directory")
    pos = 12 if ver == 1 else 28
    end = pos + tree
    data_start = end

    def cstr():
        nonlocal pos
        z = data.index(b"\0", pos)
        s = data[pos:z].decode("latin-1")
        pos = z + 1
        return s
    out = {}
    while pos < end:
        ext = cstr()
        if not ext:
            break
        while True:
            folder = cstr()
            if not folder:
                break
            while True:
                name = cstr()
                if not name:
                    break
                _crc, preload, arch, offset, ln = struct.unpack_from("<IHHII", data, pos)
                pre = data[pos + 18:pos + 18 + preload]
                pos += 18 + preload
                if arch == 0x7FFF:
                    offset += data_start
                rel = name + "." + ext if ext != " " else name
                if folder != " ":
                    rel = folder + "/" + rel
                out[rel.lower().replace("\\", "/")] = (arch, offset, ln, pre)
    return out


class AssetIndex:
    """Files the game can see. VPK listings are read once; loose folders are
    checked per lookup (they can be huge)."""

    def __init__(self, game_dir, exe_dir=None):
        self.game_dir = game_dir
        self.paths = search_paths(game_dir, exe_dir)
        self._vpk = {}
        self.errors = []

    def _listing(self, path):
        if path not in self._vpk:
            try:
                self._vpk[path] = vpk_entries(path)
            except (OSError, ValueError, struct.error) as e:
                self.errors.append(f"{path}: {e}")
                self._vpk[path] = {}
        return self._vpk[path]

    def read(self, rel, limit=None):
        """The bytes of a file (the first `limit` bytes if given), or None."""
        rel = rel.lower().replace("\\", "/").lstrip("/")
        for kind, p in self.paths:
            if kind == "vpk":
                ent = self._listing(p).get(rel)
                if ent is None:
                    continue
                arch, offset, ln, pre = ent
                if limit is not None and len(pre) >= limit:
                    return pre[:limit]
                if ln == 0:
                    return pre
                if arch == 0x7FFF:
                    src = p
                else:
                    src = p[:-len("_dir.vpk")] + "_%03d.vpk" % arch
                try:
                    with open(src, "rb") as f:
                        f.seek(offset)
                        body = f.read(ln if limit is None else max(0, min(ln, limit - len(pre))))
                except OSError:
                    return None
                return pre + body
            fp = os.path.join(p, rel)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return f.read() if limit is None else f.read(limit)
        return None

    def model_bounds(self, model):
        """(mins, maxs) of a model from its .mdl header (studiohdr_t): the view
        bounding box, or the hull when that is empty. None if unreadable."""
        key = model.lower().replace("\\", "/")
        cache = self.__dict__.setdefault("_bounds", {})
        if key not in cache:
            cache[key] = self._model_bounds(key)
        return cache[key]

    def _model_bounds(self, model):
        hdr = self.read(model, limit=156)
        if not hdr or len(hdr) < 152 or hdr[:4] != b"IDST":
            return None
        hmin = struct.unpack_from("<3f", hdr, 104)
        hmax = struct.unpack_from("<3f", hdr, 116)
        vmin = struct.unpack_from("<3f", hdr, 128)
        vmax = struct.unpack_from("<3f", hdr, 140)
        if any(abs(vmax[i] - vmin[i]) > 0.01 for i in range(3)):
            return vmin, vmax
        return hmin, hmax

    def prop_info(self, model):
        """{'static': $staticprop flag, 'prop_data': has a prop_data block,
        'allowstatic': prop_data says allowstatic 1} from the .mdl header and
        its keyvalues; None if unreadable."""
        key = model.lower().replace("\\", "/")
        cache = self.__dict__.setdefault("_propinfo", {})
        if key in cache:
            return cache[key]
        data = self.read(key)
        info = None
        if data and len(data) >= 320 and data[:4] == b"IDST":
            flags = struct.unpack_from("<i", data, 152)[0]
            kvi, kvs = struct.unpack_from("<ii", data, 312)
            text = data[kvi:kvi + kvs].decode("latin-1", "replace") if kvi and kvs > 0 else ""
            info = {"static": bool(flags & 0x10),
                    "prop_data": bool(re.search(r'"?prop_data"?\s*\{', text, re.I)),
                    "allowstatic": bool(re.search(r'"?allowstatic"?\s+"?1', text, re.I))}
        cache[key] = info
        return info

    def prop_class_problem(self, cls, model):
        """(reason, better class) when the engine would drop this model on this
        prop class, else None. vbsp deletes a prop_static whose model lacks
        $staticprop or has prop_data (without allowstatic); at spawn the game
        deletes a prop_dynamic whose model has prop_data, and a prop_physics
        whose model has none. The *_override classes skip the prop_data rule."""
        info = self.prop_info(model)
        if info is None:
            return None
        cls = cls.lower()
        pd = info["prop_data"] and not info["allowstatic"]
        if cls == "prop_static":
            if pd:
                return "has prop_data (a physics prop): vbsp deletes it", "prop_dynamic_override"
            if not info["static"]:
                return "not compiled with $staticprop: vbsp deletes it", "prop_dynamic"
        elif cls == "prop_dynamic" and info["prop_data"]:
            return "has prop_data: the game deletes it at spawn", "prop_dynamic_override"
        elif cls == "prop_physics" and not info["prop_data"]:
            return "has no prop_data: the game deletes it at spawn", "prop_physics_override"
        return None

    def static_prop_ok(self, model):
        """(ok, reason) for a prop_static; (None, reason) when unreadable."""
        if self.prop_info(model) is None:
            return None, "unreadable"
        p = self.prop_class_problem("prop_static", model)
        return (False, p[0]) if p else (True, "")

    def material_color(self, material):
        """Average colour of a material's base texture as (r, g, b) 0-255, from
        the VTF header's reflectivity (linear) converted to sRGB; None if the
        material or its texture can't be read."""
        name = material.lower().replace("\\", "/")
        for _ in range(4):                      # follow "patch" / "include" materials
            vmt = self.read("materials/" + name + ".vmt")
            if vmt is None:
                return None
            text = vmt.decode("latin-1")
            inc = re.search(r'"?include"?\s+"([^"]+)"', text, re.I)
            m = re.search(r'"?\$basetexture"?\s+"?([^"\s]+)"?', text, re.I)
            if m:
                tex = m.group(1).lower().replace("\\", "/")
                break
            if inc:
                name = inc.group(1).lower().replace("\\", "/")
                name = name[len("materials/"):] if name.startswith("materials/") else name
                name = name[:-4] if name.endswith(".vmt") else name
                continue
            return None
        else:
            return None
        hdr = self.read("materials/" + tex + ".vtf", limit=48)
        if not hdr or len(hdr) < 44 or hdr[:4] != b"VTF\0":
            return None
        r, g, b = struct.unpack_from("<3f", hdr, 32)

        def srgb(c):
            c = max(0.0, min(1.0, c))
            return int(round(255 * (c ** (1 / 2.2))))
        return srgb(r), srgb(g), srgb(b)

    def where(self, rel):
        """The search path that holds `rel` (relative path), or None."""
        rel = rel.lower().replace("\\", "/").lstrip("/")
        for kind, p in self.paths:
            if kind == "vpk":
                if rel in self._listing(p):
                    return p
            elif os.path.isfile(os.path.join(p, rel)):
                return p
        return None

    def exists(self, rel):
        return self.where(rel) is not None

    def match(self, pattern, limit=None):
        """[(rel, source)] for every file matching the glob pattern (first source wins)."""
        pat = pattern.lower().replace("\\", "/").lstrip("/")
        seen = {}
        for kind, p in self.paths:
            if kind == "vpk":
                for rel in self._listing(p):
                    if rel not in seen and fnmatch.fnmatchcase(rel, pat):
                        seen[rel] = p
            else:
                for f in glob.glob(os.path.join(p, pat), recursive="**" in pat):
                    if os.path.isfile(f):
                        rel = os.path.relpath(f, p).replace("\\", "/").lower()
                        seen.setdefault(rel, p)
            if limit and len(seen) >= limit * 4:
                break
        out = sorted(seen.items())
        return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# What a map uses
# ---------------------------------------------------------------------------

SKY_SUFFIXES = ("up", "dn", "lf", "rt", "ft", "bk")


def used_assets(doc):
    """{'materials': {name: count}, 'models': {...}, 'scripts': {...},
    'soundscapes': {...}, 'sky': name or None}."""
    mats, models, scripts, scapes, statics = {}, {}, {}, {}, {}
    for e in vmf.iter_entities(doc):
        c = (e.get("classname") or "").lower()
        if c in ("prop_static", "prop_dynamic", "prop_physics") and (e.get("model") or "").lower().endswith(".mdl"):
            k = (c, e.get("model").lower())
            statics[k] = statics.get(k, 0) + 1
        for s in (e.get("vscripts") or "").split():
            s = s.lower().replace("\\", "/")
            scripts[s] = scripts.get(s, 0) + 1
        if (e.get("classname") or "").lower().startswith("env_soundscape") and e.get("soundscape"):
            n = e.get("soundscape")
            scapes[n] = scapes.get(n, 0) + 1
    for root in doc.roots:
        if root.name not in ("world", "entity"):
            continue
        for n in vmf.iter_all(root):
            if n.name == "side":
                m = (n.get("material") or "").strip()
                if m:
                    mats[m.upper()] = mats.get(m.upper(), 0) + 1
            elif n.name in ("entity", "world"):
                mdl = n.get("model") or ""
                if mdl.lower().endswith(".mdl"):
                    models[mdl.lower()] = models.get(mdl.lower(), 0) + 1
    world = doc.top("world")
    sky = world.get("skyname") if world is not None else None
    return {"materials": mats, "models": models, "scripts": scripts, "soundscapes": scapes, "sky": sky,
            "static_props": statics}


def soundscape_names(index):
    """Lower-case names of every soundscape defined in scripts/soundscapes*.txt."""
    names = set()
    for rel, _src in index.match("scripts/soundscapes*.txt"):
        text = (index.read(rel) or b"").decode("latin-1")
        depth = 0
        for line in text.splitlines():
            line = _strip_comment(line).strip()
            if depth == 0:
                m = re.match(r'^"([^"]+)"$', line)
                if m:
                    names.add(m.group(1).lower())
            depth += line.count("{") - line.count("}")
    return names


def missing_assets(doc, index):
    """[(kind, name, uses)] for materials / models / sky files the game can't find."""
    used = used_assets(doc)
    out = []
    for m, c in sorted(used["materials"].items()):
        if not index.exists("materials/" + m.lower() + ".vmt"):
            out.append(("material", m, c))
    for m, c in sorted(used["models"].items()):
        if not index.exists(m):
            out.append(("model", m, c))
    for (cls, m), c in sorted(used["static_props"].items()):
        p = index.prop_class_problem(cls, m)
        if p:
            out.append(("propclass", f"{cls} {m} {p[0]}; use {p[1]}", c))
    if used["sky"]:
        for s in SKY_SUFFIXES:
            if not index.exists(f"materials/skybox/{used['sky'].lower()}{s}.vmt"):
                out.append(("sky", used["sky"] + s, 1))
                break
    for s, c in sorted(used["scripts"].items()):
        rel = "scripts/vscripts/" + s
        if not (index.exists(rel) or index.exists(rel + ".nut")):
            out.append(("vscript", s, c))
    if used["soundscapes"]:
        known = soundscape_names(index)
        for s, c in sorted(used["soundscapes"].items()):
            if s.lower() not in known:
                out.append(("soundscape", s, c))
    return out


def index_for(args, map_path=None):
    """An AssetIndex from the same path settings compile/shoot use, or None."""
    import vmf_game
    p = vmf_game.resolve(args, map_path)
    if not p.game_dir:
        return None
    exe_dir = os.path.dirname(p.game_exe) if p.game_exe else None
    return AssetIndex(p.game_dir, exe_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_n(v):
    r = round(v)
    return str(int(r)) if abs(v - r) < 0.05 else "%.1f" % v


def color_name(c):
    """A rough word for an sRGB colour, to scan --color output quickly."""
    r, g, b = (v / 255.0 for v in c)
    mx, mn = max(r, g, b), min(r, g, b)
    light = (mx + mn) / 2
    sat = 0 if mx == mn else (mx - mn) / (1 - abs(2 * light - 1) + 1e-9)
    tone = "dark" if light < 0.3 else "light" if light > 0.7 else ""
    if sat < 0.15:
        return ("black" if light < 0.12 else "white" if light > 0.88 else (tone + " grey").strip())
    if mx == r:
        h = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        h = (b - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    h *= 60
    hue = ("red" if h < 15 or h >= 345 else "orange" if h < 40 else "yellow" if h < 65 else "green" if h < 160
           else "cyan" if h < 195 else "blue" if h < 255 else "purple" if h < 300 else "pink")
    if hue in ("orange", "yellow") and light < 0.45:
        hue = "brown"
    return (tone + " " + hue).strip()


def _cmd_assets(args):
    index = index_for(args, args.missing)
    if index is None:
        print("could not find the game dir: pass --game-dir or a map inside the game tree "
              "(or set game_dir in vmf.local.json)", file=sys.stderr)
        sys.exit(2)
    if args.paths:
        for kind, p in index.paths:
            print(f"  {kind:3} {p}")
    if args.missing:
        doc = vmf.load(args.missing)
        miss = missing_assets(doc, index)
        used = used_assets(doc)
        print(f"{args.missing}: {len(used['materials'])} materials, {len(used['models'])} models, "
              f"sky {used['sky'] or '-'}")
        if not miss:
            print("OK   everything is found")
        for kind, name, c in miss:
            print(f"MISS {kind:8} {name}  ({c} use{'s' if c != 1 else ''})")
        for e in index.errors:
            print(f"warn {e}", file=sys.stderr)
        if miss:
            sys.exit(1)
        return
    if not args.pattern:
        if not args.paths:
            print("give a PATTERN (e.g. 'materials/dev/*.vmt'), --missing MAP.vmf or --paths", file=sys.stderr)
            sys.exit(2)
        return
    for pat in args.pattern:
        hits = index.match(pat, args.limit)
        print(f"{pat}: {len(hits)} match(es){' (limited)' if args.limit and len(hits) >= args.limit else ''}")
        for rel, src in hits:
            extra = ""
            if args.color and rel.endswith(".vmt") and rel.startswith("materials/"):
                c = index.material_color(rel[len("materials/"):-4])
                extra = "   #%02x%02x%02x %s" % (c + (color_name(c),)) if c else "   (no base texture)"
            if args.size and rel.endswith(".mdl"):
                b = index.model_bounds(rel)
                if b:
                    mn, mx = b
                    extra += "   %s x %s x %s  (x %s..%s, y %s..%s, z %s..%s)" % tuple(
                        _fmt_n(v) for v in (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2],
                                            mn[0], mx[0], mn[1], mx[1], mn[2], mx[2]))
                else:
                    extra += "   (no bounds)"
            print(f"  {rel}{extra}" + (f"   [{os.path.basename(src)}]" if args.where else ""))
    for e in index.errors:
        print(f"warn {e}", file=sys.stderr)


def add_parsers(sub):
    sp = sub.add_parser("assets", help="find materials/models/sounds the game can load; --missing MAP checks a map")
    sp.add_argument("pattern", nargs="*", help="glob relative to the game root, e.g. 'materials/metal/*fence*.vmt'")
    sp.add_argument("--missing", metavar="MAP.vmf", help="list the materials / models / sky a map uses that aren't found")
    sp.add_argument("--limit", type=int, default=60, help="max matches per pattern (0 = all)")
    sp.add_argument("--where", action="store_true", help="show which search path each file comes from")
    sp.add_argument("--color", action="store_true",
                    help="for materials: the base texture's average colour (from the VTF header)")
    sp.add_argument("--size", action="store_true",
                    help="for models: bounding box size and extents around the model origin (from the .mdl header)")
    sp.add_argument("--paths", action="store_true", help="print the search paths used")
    sp.add_argument("--game-dir", help="mod/game folder with gameinfo.txt (env VMF_GAME_DIR)")
    sp.add_argument("--game-exe", help="game executable, e.g. hl2.exe (env VMF_GAME_EXE)")
    sp.set_defaults(func=_cmd_assets, bin_dir=None)
