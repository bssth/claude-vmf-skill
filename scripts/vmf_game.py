#!/usr/bin/env python3
"""vmf_game.py - compile a VMF and look at it in the game (Source SDK 2013 style).

  compile     run vbsp / vvis / vrad on a map and summarise what went wrong
              (leaks with the pointfile, errors and warnings grouped by kind)
  shoot       start the game on a compiled map, put the camera at given views,
              save one screenshot per view, quit
  game-paths  show which compile tools, game dir and game exe would be used

Where things are, in order of precedence:
  1. flags        --bin-dir, --game-dir, --game-exe
  2. env vars     VMF_BIN_DIR, VMF_GAME_DIR, VMF_GAME_EXE
  3. JSON config  $VMF_CONFIG, or vmf.local.json next to SKILL.md (gitignored):
                  {"bin_dir": "...", "game_dir": "...", "game_exe": "..."}
  4. auto         game dir = nearest folder above the map holding gameinfo.txt;
                  tools = <game dir>/../bin or <game exe dir>/bin;
                  game exe = <game dir>/../hl2.exe, else the Source SDK Base 2013
                  install found through Steam's library folders (by the
                  SteamAppId in gameinfo.txt).

Screenshots are taken by talking to the running game with `hl2.exe -hijack`
(the same mechanism Hammer uses), one view at a time, so each file is known to
belong to its view. cfg/config.cfg is restored afterwards because the cheats and
HUD toggles used for clean shots would otherwise be saved into it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import vmf

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(SKILL_DIR, ".state")

_SDK_BASE_BY_APPID = {
    "243730": "Source SDK Base 2013 Singleplayer",
    "243750": "Source SDK Base 2013 Multiplayer",
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def load_config():
    path = os.environ.get("VMF_CONFIG") or os.path.join(SKILL_DIR, "vmf.local.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), path
    return {}, None


def find_game_dir(start):
    d = os.path.dirname(os.path.abspath(start)) if os.path.isfile(start) else os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "gameinfo.txt")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _steam_libraries():
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except (ImportError, OSError):
        pass
    roots += [os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]
    libs = []
    for r in roots:
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if not os.path.isfile(vdf):
            continue
        libs.append(r)
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                libs.append(m.group(1).replace("\\\\", "\\"))
    seen, out = set(), []
    for lib in libs:
        key = os.path.normcase(os.path.abspath(lib))
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def _gameinfo_appid(game_dir):
    try:
        with open(os.path.join(game_dir, "gameinfo.txt"), "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r"SteamAppId\s+\"?(\d+)", f.read(), re.I)
            return m.group(1) if m else None
    except OSError:
        return None


class Paths:
    def __init__(self):
        self.game_dir = self.bin_dir = self.game_exe = None
        self.src = {}

    def require(self, *names):
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            hints = {
                "game_dir": "--game-dir / VMF_GAME_DIR (the folder with gameinfo.txt)",
                "bin_dir": "--bin-dir / VMF_BIN_DIR (the folder with vbsp.exe)",
                "game_exe": "--game-exe / VMF_GAME_EXE (hl2.exe)",
            }
            for n in missing:
                print(f"could not find {n}: set {hints[n]}", file=sys.stderr)
            sys.exit(2)


def resolve(args, map_path=None):
    cfg, cfg_path = load_config()
    p = Paths()

    def pick(key, env):
        v = getattr(args, key, None)
        if v:
            return v, "flag"
        v = os.environ.get(env)
        if v:
            return v, "env " + env
        v = cfg.get(key)
        if v:
            return v, "config " + str(cfg_path)
        return None, None

    p.game_dir, p.src["game_dir"] = pick("game_dir", "VMF_GAME_DIR")
    p.bin_dir, p.src["bin_dir"] = pick("bin_dir", "VMF_BIN_DIR")
    p.game_exe, p.src["game_exe"] = pick("game_exe", "VMF_GAME_EXE")

    if not p.game_dir and map_path:
        p.game_dir = find_game_dir(map_path)
        p.src["game_dir"] = "auto: gameinfo.txt above the map" if p.game_dir else None

    if not p.game_exe and p.game_dir:
        cand = os.path.join(os.path.dirname(os.path.abspath(p.game_dir)), "hl2.exe")
        if os.path.isfile(cand):
            p.game_exe, p.src["game_exe"] = cand, "auto: next to the game dir"
        else:
            name = _SDK_BASE_BY_APPID.get(_gameinfo_appid(p.game_dir) or "")
            names = [name] if name else list(_SDK_BASE_BY_APPID.values())
            for lib in _steam_libraries():
                for n in names:
                    cand = os.path.join(lib, "steamapps", "common", n, "hl2.exe")
                    if os.path.isfile(cand):
                        p.game_exe, p.src["game_exe"] = cand, "auto: Steam library"
                        break
                if p.game_exe:
                    break

    if not p.bin_dir:
        cands = []
        if p.game_dir:
            cands.append(("auto: <game dir>/../bin", os.path.join(os.path.dirname(os.path.abspath(p.game_dir)), "bin")))
        if p.game_exe:
            cands.append(("auto: <game exe dir>/bin", os.path.join(os.path.dirname(p.game_exe), "bin")))
        for why, c in cands:
            if os.path.isfile(os.path.join(c, "vbsp.exe")):
                p.bin_dir, p.src["bin_dir"] = c, why
                break
    if p.game_dir:
        p.game_dir = os.path.abspath(p.game_dir)
    return p


def _cmd_paths(args):
    p = resolve(args, args.map)
    for key in ("game_dir", "bin_dir", "game_exe"):
        v = getattr(p, key)
        ok = v and os.path.exists(v)
        print(f"{key:9} {v or '-'}  [{p.src.get(key) or 'not found'}]{'' if ok or not v else '  (MISSING)'}")
    if p.bin_dir:
        for t in ("vbsp.exe", "vvis.exe", "vrad.exe"):
            print(f"          {t}: {'ok' if os.path.isfile(os.path.join(p.bin_dir, t)) else 'missing'}")
    cfg, cfg_path = load_config()
    print(f"config    {cfg_path or '(none; ' + os.path.join(SKILL_DIR, 'vmf.local.json') + ' would be read)'}")


# ---------------------------------------------------------------------------
# Installing a bsp into <game>/maps without clobbering someone else's file
# ---------------------------------------------------------------------------

def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _state():
    path = os.path.join(STATE_DIR, "installed.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), path
    except (OSError, ValueError):
        return {}, path


def install_bsp(bsp, game_dir, overwrite=False):
    """Copy a bsp into <game>/maps. An existing file there is replaced only if
    this tool put it there (same hash as recorded) or with overwrite=True."""
    maps = os.path.join(game_dir, "maps")
    os.makedirs(maps, exist_ok=True)
    dst = os.path.join(maps, os.path.basename(bsp))
    if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(bsp)):
        return dst
    state, state_path = _state()
    key = os.path.normcase(os.path.abspath(dst))
    if os.path.exists(dst) and not overwrite and state.get(key) != _sha1(dst):
        print(f"refusing to replace {dst}: it wasn't installed by this tool "
              f"(pass --overwrite if replacing it is intended)", file=sys.stderr)
        sys.exit(1)
    shutil.copy2(bsp, dst)
    state[key] = _sha1(dst)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    return dst


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

_IGNORE = re.compile(
    r"^(materialPath:|Loading |ConVarRef |Patching WVT|fixing up env_cubemap|\s*!? ?Run buildcubemaps|"
    r"Creating default (LDR|HDR) cubemaps|\s+skybox/|Valve Software|Command line|Building |Processing |"
    r"Chop Details|Find Visible|Merged \d+|Merging details|FixTjuncs|PruneNodes|WriteBSP|"
    r"done \(|writing |Finding |Displacement Alpha|Setting up|Using |\d+ threads|"
    r"Compacting|Static prop lighting|Computing static prop|BuildFacelights|BuildVisLeafs|"
    r"MakeAllScales|SparseMatrix|Transfer|Bounce|ThreadComputeLeafAmbient|Ready to Finish|"
    r"Object names|-+|Can't find steam\.dll|\s*$)", re.I)
# vrad's "Object names / Objects/Maxobjs / Memory / Maxmem / Fullness" table
_LIMIT = re.compile(r"^(\w[\w ]*?)\s+(\d+|\[variable\])(?:/(\d+))?\s+(\d+)/(\d+)\s+\(\s*([\d.]+)%\)")
_LEAK = re.compile(r"Entity (\S+) \(\s*(-?[\d.]+) (-?[\d.]+) (-?[\d.]+)\s*\) leaked", re.I)
_ERR = re.compile(r"\*\*\*\* leaked|\berror\b|MAX_MAP_|exceed|overflow|couldn't|could not|"
                  r"can't (?!shrink)|failed|fatal|\bbad\b", re.I)
_WARN = re.compile(r"warning|microbrush|outside the (map|world)|can't shrink|duplicate plane|"
                   r"pointing at deleted|not found|missing|mixed face|too many|invalid|ignoring", re.I)
_ELAPSED = re.compile(r"(?:(\d+) minutes?, )?(\d+) seconds? elapsed")


def summarize_output(text):
    """Group the interesting lines of a tool's output: severity, normalized
    pattern (numbers -> #), count, first example."""
    groups = {}
    for raw in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"^(\d\.\.\.)+\d*", "", raw).strip()  # progress dots glued to text
        if not line or _IGNORE.match(line) or _ELAPSED.search(line) or _LIMIT.match(line):
            continue
        sev = "ERROR" if _ERR.search(line) else "WARN" if _WARN.search(line) else None
        if not sev:
            continue
        key = re.sub(r"-?\d+(\.\d+)?", "#", line)
        g = groups.setdefault(key, {"sev": sev, "count": 0, "example": line})
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: (g["sev"] != "ERROR", -g["count"]))


def parse_limits(text):
    """[(name, used, max, percent)] from vrad's BSP fullness table. For
    [variable] lumps (lightdata, pakfile, ...) used/max are memory bytes."""
    out = []
    for raw in text.replace("\r", "\n").split("\n"):
        m = _LIMIT.match(raw.strip())
        if m and m.group(3):
            out.append((m.group(1).strip(), int(m.group(2)), int(m.group(3)), float(m.group(6))))
        elif m:
            out.append((m.group(1).strip(), int(m.group(4)), int(m.group(5)), float(m.group(6))))
    return out


def _run(exe, argv, cwd, echo):
    t0 = time.time()
    try:
        proc = subprocess.Popen([exe] + argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        return -1, f"could not start {exe}: {e}", 0.0
    chunks = []
    for line in iter(proc.stdout.readline, b""):
        chunks.append(line)
        if echo:
            sys.stdout.write(line.decode("latin-1").encode("ascii", "replace").decode("ascii"))
            sys.stdout.flush()
    proc.wait()
    return proc.returncode, b"".join(chunks).decode("latin-1"), time.time() - t0


def _cmd_compile(args):
    vmf_path = os.path.abspath(args.file)
    if not vmf_path.lower().endswith(".vmf") or not os.path.isfile(vmf_path):
        print(f"not a .vmf file: {args.file}", file=sys.stderr)
        sys.exit(2)
    paths = resolve(args, vmf_path)
    paths.require("game_dir", "bin_dir")
    base = vmf_path[:-4]
    bsp, lin = base + ".bsp", base + ".lin"
    game = ["-game", paths.game_dir]
    threads = ["-threads", str(args.threads)] if args.threads else []

    steps = []
    if args.entities_only:
        if not os.path.isfile(bsp):
            print("--entities-only needs an existing .bsp next to the map", file=sys.stderr)
            sys.exit(2)
        steps.append(("vbsp", ["-onlyents"] + game + [base]))
    else:
        steps.append(("vbsp", threads + game + [base]))
        if not args.no_vis:
            steps.append(("vvis", (["-fast"] if args.mode == "fast" else []) + threads + game + [base]))
        if not args.no_rad:
            rad = {"fast": ["-fast", "-bounce", "2", "-noextra"], "normal": [], "final": ["-final"]}[args.mode]
            steps.append(("vrad", rad + ["-ldr"] + threads + game + [base]))
            if args.hdr or args.mode == "final":
                steps.append(("vrad", rad + ["-hdr"] + threads + game + [base]))

    lin_before = os.path.getmtime(lin) if os.path.exists(lin) else None
    results = []
    leaked = None
    total_t0 = time.time()
    for tool, argv in steps:
        if tool == "vvis" and leaked:
            results.append((tool, None, "", 0.0, "skipped: the map leaks, so vbsp wrote no portal file"))
            continue
        exe = os.path.join(paths.bin_dir, tool + ".exe")
        print(f"== {tool} {' '.join(argv)}", flush=True)
        rc, out, dt = _run(exe, argv, paths.bin_dir, args.verbose)
        if tool == "vbsp":
            m = _LEAK.search(out)
            lin_new = os.path.exists(lin) and os.path.getmtime(lin) != lin_before \
                and os.path.getmtime(lin) >= total_t0 - 1
            if m or "**** leaked ****" in out or lin_new:
                leaked = {"entity": m.group(1) if m else "?",
                          "pos": tuple(float(m.group(i)) for i in (2, 3, 4)) if m else None,
                          "lin": lin if lin_new else None}
        results.append((tool, rc, out, dt, ""))
        if rc != 0 and tool == "vbsp":
            break

    # summary
    print()
    ok = True
    limits = []
    for tool, rc, out, dt, note in results:
        if rc is None:
            print(f"{tool:5} SKIPPED  {note}")
            continue
        status = "ok" if rc == 0 else f"FAILED (exit {rc})"
        ok &= rc == 0
        print(f"{tool:5} {status}  {dt:.1f}s")
        for g in summarize_output(out)[: args.limit]:
            cnt = f" (x{g['count']})" if g["count"] > 1 else ""
            print(f"      {g['sev']:5} {g['example'][:160]}{cnt}")
        limits = parse_limits(out) or limits
    if limits:
        top = sorted(limits, key=lambda t: -t[3])
        print("limits (fullest): " + ", ".join(f"{n} {p:g}%" for n, _u, _m, p in top[:4]))
        for n, u, mx, p in top:
            if p >= 60:
                print(f"      {'ERROR' if p >= 100 else 'WARN ':5} {n}: {u}/{mx} ({p:g}%)")
                ok &= p < 100
        counts = {n: u for n, u, _m, _p in limits}
        print("size: " + ", ".join(f"{k} {counts[k]}" for k in
                                   ("brushes", "brushsides", "faces", "leaves", "LDR worldlights", "areas")
                                   if k in counts))
    if leaked:
        ok = False
        pos = " at (%g %g %g)" % leaked["pos"] if leaked["pos"] else ""
        print(f"\nLEAK: entity {leaked['entity']}{pos} can see the void.")
        if leaked["lin"]:
            print(f"      pointfile {leaked['lin']}")
            print(f"      look at it: python vmf.py render {args.file} --leak {leaked['lin']} --focus")
    if os.path.isfile(bsp) and not args.entities_only and results and results[0][1] == 0:
        print(f"\nbsp  {bsp}")
        if args.install:
            dst = install_bsp(bsp, paths.game_dir, args.overwrite)
            print(f"installed to {dst}")
    print(f"full output: {base}.log (the tools append to it, like a Hammer compile)")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# shoot
# ---------------------------------------------------------------------------

def parse_views(path):
    """Views file: one view per line, `name x y z pitch yaw [roll]` (eye
    position, Source angles); blank lines and # comments are ignored."""
    views = []
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            views.append(parse_view(line, f"{path}:{n}"))
    return views


def parse_view(text, where="--view"):
    parts = text.replace(",", " ").split()
    try:
        nums = [float(v) for v in parts[1:]]
    except ValueError:
        nums = []
    if len(nums) not in (5, 6):
        print(f"{where}: want 'name x y z pitch yaw [roll]', got {text!r}", file=sys.stderr)
        sys.exit(2)
    roll = nums[5] if len(nums) == 6 else 0.0
    return {"name": parts[0], "pos": tuple(nums[0:3]), "ang": (nums[3], nums[4], roll)}


def auto_views(doc):
    """The first info_player_start looking 4 ways (eye height 64), plus every
    point_viewcontrol and every entity whose targetname starts with shot_
    (drop an info_target named shot_<what> with angles to add a view)."""
    from vmf_render import parse_vec
    views, have_start = [], False
    for e in vmf.iter_entities(doc):
        cls = (e.get("classname") or "").lower()
        name = e.get("targetname") or ""
        o = parse_vec(e.get("origin"))
        if o is None:
            continue
        a = parse_vec(e.get("angles")) or (0.0, 0.0, 0.0)
        if cls == "info_player_start" and not have_start:
            have_start = True
            for dy in (0, 90, 180, 270):
                yaw = (a[1] + dy) % 360
                views.append({"name": "start_yaw%d" % yaw, "pos": (o[0], o[1], o[2] + 64), "ang": (0.0, yaw, 0.0)})
        elif cls == "point_viewcontrol" or name.lower().startswith("shot_"):
            views.append({"name": name or cls, "pos": o, "ang": a})
    return views


def _running(exe_name):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        return exe_name.lower() in out.lower()
    except OSError:
        return False


class _ConsoleLog:
    """Reads only what the game appends to console.log after we start."""

    def __init__(self, path):
        self.path = path
        self.start = os.path.getsize(path) if os.path.exists(path) else 0

    def text(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.start)
                return f.read().decode("latin-1")
        except OSError:
            return ""


def _fmt(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


_NUM = r"(-?[\d.]+)"
_GETPOS = re.compile(r"setpos %s %s %s;setang %s %s %s" % ((_NUM,) * 6))


def _cmd_shoot(args):
    target = args.map
    vmf_path = args.vmf
    if target.lower().endswith(".vmf"):
        vmf_path = vmf_path or target
        bsp = target[:-4] + ".bsp"
    elif target.lower().endswith(".bsp"):
        bsp = target
        if not vmf_path and os.path.isfile(target[:-4] + ".vmf"):
            vmf_path = target[:-4] + ".vmf"
    else:
        bsp = None
    paths = resolve(args, vmf_path or bsp)
    paths.require("game_dir", "game_exe")
    if bsp is None:
        bsp = os.path.join(paths.game_dir, "maps", target + ".bsp")
    if not os.path.isfile(bsp):
        print(f"no compiled map at {bsp} (run compile first)", file=sys.stderr)
        sys.exit(2)
    mapname = os.path.splitext(os.path.basename(bsp))[0]
    installed = install_bsp(bsp, paths.game_dir, args.overwrite)

    views = []
    for vf in args.views or []:
        views += parse_views(vf)
    for v in args.view or []:
        views.append(parse_view(v))
    if not views:
        if not vmf_path:
            print("no views: pass --views FILE / --view '...', or a .vmf for automatic views", file=sys.stderr)
            sys.exit(2)
        views = auto_views(vmf.load(vmf_path))
        if not views:
            print("no automatic views (no info_player_start / point_viewcontrol / shot_* entity)", file=sys.stderr)
            sys.exit(2)

    exe = paths.game_exe
    exe_name = os.path.basename(exe)
    if _running(exe_name):
        print(f"{exe_name} is already running; close the game first (shoot needs its own instance)",
              file=sys.stderr)
        sys.exit(1)
    if os.name == "nt" and not _running("steam.exe"):
        print("note: Steam doesn't seem to be running; the game may refuse to start", file=sys.stderr)

    game = paths.game_dir
    cfg_dir = os.path.join(game, "cfg")
    os.makedirs(cfg_dir, exist_ok=True)
    config_cfg = os.path.join(cfg_dir, "config.cfg")
    config_backup = open(config_cfg, "rb").read() if os.path.isfile(config_cfg) else None
    setup_cfg = os.path.join(cfg_dir, "vmfshoot_setup.cfg")
    view_cfg = os.path.join(cfg_dir, "vmfshoot_view.cfg")
    shots_dir = os.path.join(game, "screenshots")
    shots_existed = os.path.isdir(shots_dir)
    seen = set(os.listdir(shots_dir)) if shots_existed else set()
    out_dir = os.path.abspath(args.out or f"shots_{mapname}")
    os.makedirs(out_dir, exist_ok=True)
    log = _ConsoleLog(os.path.join(game, "console.log"))

    # The game window takes focus when it starts, so mouse / keyboard / gamepad
    # input would turn or fly the noclip camera between setpos and the shot:
    # zero all of it (the archived ones come back with config.cfg).
    setup = ["sv_cheats 1", "hideconsole", "developer 0", "con_drawnotify 0", "god", "notarget", "noclip",
             "sensitivity 0", "joystick 0", "sv_noclipspeed 0", "sv_noclipaccelerate 0",
             "r_drawviewmodel 0", "crosshair 0", "jpeg_quality %d" % args.quality]
    if not args.hud:
        setup.append("cl_drawhud 0")
    if not args.live_ai:
        setup.append("ai_disable 1")
    if args.fullbright:
        setup.append("mat_fullbright 1")
    if args.fov:
        setup.append("fov_desired %d" % args.fov)
    setup.append("echo VMFSHOOT_SETUP_DONE")
    with open(setup_cfg, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(setup) + "\n")

    w, _, h = args.size.lower().partition("x")
    cmd = [exe, "-game", game, "-windowed", "-w", w, "-h", h, "-novid", "-nosound", "-nojoy",
           "-condebug", "+map", mapname] + (args.extra_args.split() if args.extra_args else [])

    def hijack(*cmds):
        subprocess.run([exe, "-hijack", "-game", game] + list(cmds), timeout=60)

    def wait_log(marker, timeout, proc):
        t0 = time.time()
        while time.time() - t0 < timeout:
            txt = log.text()
            if marker in txt:
                return True
            for bad in ("Couldn't find map", "map load failed", "Host_Error", "Missing map"):
                if bad in txt:
                    raise RuntimeError(f"game reported '{bad}'")
            if proc.poll() is not None:
                raise RuntimeError(f"game exited early (code {proc.returncode})")
            time.sleep(0.5)
        return False

    results = []
    proc = None
    t_start = time.time()
    try:
        print(f"starting {exe_name} on {mapname} ({len(views)} views) ...", flush=True)
        proc = subprocess.Popen(cmd)
        if not wait_log("Redownloading all lightmaps", args.load_timeout, proc):
            raise RuntimeError(f"map didn't finish loading in {args.load_timeout}s")
        print(f"map loaded in {time.time() - t_start:.0f}s", flush=True)
        time.sleep(1.0)
        hijack("+exec", "vmfshoot_setup")
        if not wait_log("VMFSHOOT_SETUP_DONE", 30, proc):
            raise RuntimeError("the game didn't run the setup cfg (is -hijack supported?)")
        time.sleep(args.start_delay)
        def take(v):
            """Move the camera, shoot, and read back where the camera really was.
            Returns (screenshot file name or None, camera tuple or None)."""
            x, y, z = v["pos"]
            p, yw, r = v["ang"]
            with open(view_cfg, "w", encoding="ascii", newline="\n") as f:
                f.write("setpos %s %s %s\nsetang %s %s %s\n"
                        % (_fmt(x), _fmt(y), _fmt(z - args.eye_height), _fmt(p), _fmt(yw), _fmt(r)))
            hijack("+exec", "vmfshoot_view")
            time.sleep(args.settle)
            log_mark = len(log.text())
            hijack("+getpos", "+jpeg")  # getpos prints the eye position/angles the shot is taken from
            t0 = time.time()
            while time.time() - t0 < 15:
                time.sleep(0.25)
                if not os.path.isdir(shots_dir):
                    continue
                new = sorted(n for n in os.listdir(shots_dir) if n not in seen and n.lower().endswith(".jpg"))
                if new:
                    f0 = os.path.join(shots_dir, new[0])
                    size = os.path.getsize(f0)
                    time.sleep(0.3)
                    if size > 0 and os.path.getsize(f0) == size:
                        seen.add(new[0])
                        cam = _GETPOS.findall(log.text()[log_mark:])
                        return new[0], (tuple(float(c) for c in cam[-1]) if cam else None)
            return None, None

        def off_by(v, cam):
            (x, y, z), (p, yw, _r) = v["pos"], v["ang"]
            dpos = max(abs(cam[0] - x), abs(cam[1] - y), abs(cam[2] - z))
            dang = max(abs((cam[3] - p + 180) % 360 - 180), abs((cam[4] - yw + 180) % 360 - 180))
            return dpos > 2 or dang > 2

        for i, v in enumerate(views):
            for attempt in range(3):
                got, cam = take(v)
                if got is None or cam is None or not off_by(v, cam):
                    break
                os.remove(os.path.join(shots_dir, got))  # the camera drifted; shoot again
            if got is None:
                results.append((v, None))
                print(f"  {v['name']}: no screenshot appeared", flush=True)
                continue
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", v["name"])
            dst = os.path.join(out_dir, "%02d_%s.jpg" % (i + 1, safe))
            shutil.move(os.path.join(shots_dir, got), dst)
            results.append((v, dst))
            where = ""
            if cam:
                where = "  eye (%s %s %s) ang (%s %s %s)" % tuple(_fmt(round(c, 1)) for c in cam)
                if off_by(v, cam):
                    where += "  WARN: camera is not where the view asked for"
            print(f"  {v['name']}: {dst}{where}", flush=True)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        tail = log.text().strip().splitlines()[-15:]
        if tail:
            print("last console lines:\n  " + "\n  ".join(t[:160] for t in tail), file=sys.stderr)
    finally:
        if proc is not None and proc.poll() is None and not args.keep_open:
            try:
                hijack("+quit")
                proc.wait(30)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill()
        if not args.keep_open:
            for f in (setup_cfg, view_cfg):
                if os.path.exists(f):
                    os.remove(f)
            if config_backup is not None:
                with open(config_cfg, "rb") as f:
                    if f.read() != config_backup:
                        with open(config_cfg, "wb") as g:
                            g.write(config_backup)
            if not shots_existed and os.path.isdir(shots_dir) and not os.listdir(shots_dir):
                os.rmdir(shots_dir)

    ok = [d for _v, d in results if d]
    print(f"\n{len(ok)}/{len(views)} screenshots in {out_dir}  ({time.time() - t_start:.0f}s total)")
    if installed != os.path.abspath(bsp):
        print(f"map installed at {installed}")
    if args.keep_open:
        print("game left running (--keep-open); cfg/config.cfg was NOT restored")
    sys.exit(0 if len(ok) == len(views) else 1)


# ---------------------------------------------------------------------------
# CLI wiring (called from vmf.py)
# ---------------------------------------------------------------------------

def _add_path_args(sp):
    sp.add_argument("--game-dir", help="mod/game folder with gameinfo.txt (env VMF_GAME_DIR)")
    sp.add_argument("--bin-dir", help="folder with vbsp/vvis/vrad (env VMF_BIN_DIR)")
    sp.add_argument("--game-exe", help="game executable, e.g. hl2.exe (env VMF_GAME_EXE)")


def add_parsers(sub):
    sp = sub.add_parser("compile", help="run vbsp/vvis/vrad and summarise leaks, errors and warnings")
    sp.add_argument("file")
    sp.add_argument("--mode", choices=("fast", "normal", "final"), default="fast",
                    help="fast: vvis -fast, vrad -fast -bounce 2 (default); final: full vis, vrad -final, LDR+HDR")
    sp.add_argument("--no-vis", action="store_true", help="skip vvis")
    sp.add_argument("--no-rad", action="store_true", help="skip vrad (the map renders fullbright)")
    sp.add_argument("--hdr", action="store_true", help="also compile HDR lighting")
    sp.add_argument("--entities-only", action="store_true", help="vbsp -onlyents: update entities in an existing bsp")
    sp.add_argument("--threads", type=int)
    sp.add_argument("--install", action="store_true", help="copy the bsp into <game>/maps")
    sp.add_argument("--overwrite", action="store_true", help="with --install: replace a map this tool didn't install")
    sp.add_argument("--limit", type=int, default=12, help="issue groups shown per tool")
    sp.add_argument("--verbose", action="store_true", help="stream the tools' output")
    _add_path_args(sp)
    sp.set_defaults(func=_cmd_compile)

    sp = sub.add_parser("shoot", help="screenshot a compiled map in the game from given views")
    sp.add_argument("map", help="MAP.vmf (uses the .bsp next to it), MAP.bsp, or a map name in <game>/maps")
    sp.add_argument("--views", action="append", metavar="FILE", help="views file: 'name x y z pitch yaw [roll]' per line")
    sp.add_argument("--view", action="append", metavar="'NAME X Y Z PITCH YAW'", help="one view (repeatable)")
    sp.add_argument("--vmf", help="VMF for automatic views when MAP is a bsp or a name")
    sp.add_argument("--out", help="output folder (default ./shots_<map>)")
    sp.add_argument("--size", default="1280x720", help="window size WxH (default 1280x720)")
    sp.add_argument("--fov", type=int, help="fov_desired for the shots")
    sp.add_argument("--fullbright", action="store_true", help="mat_fullbright 1 (for maps compiled without vrad)")
    sp.add_argument("--hud", action="store_true", help="keep the HUD")
    sp.add_argument("--live-ai", action="store_true", help="don't freeze NPCs (ai_disable)")
    sp.add_argument("--eye-height", type=float, default=64.0, help="view positions are eyes; feet = eye - this")
    sp.add_argument("--settle", type=float, default=0.6, help="seconds to wait after moving the camera")
    sp.add_argument("--start-delay", type=float, default=1.5, help="seconds to wait after the map loads (fades)")
    sp.add_argument("--load-timeout", type=float, default=180)
    sp.add_argument("--quality", type=int, default=92, help="jpeg_quality")
    sp.add_argument("--overwrite", action="store_true", help="replace a same-named map in <game>/maps")
    sp.add_argument("--keep-open", action="store_true", help="leave the game running afterwards")
    sp.add_argument("--extra-args", help="extra game command line arguments")
    _add_path_args(sp)
    sp.set_defaults(func=_cmd_shoot)

    sp = sub.add_parser("game-paths", help="show which compile tools / game dir / game exe would be used")
    sp.add_argument("map", nargs="?", help="a map path to auto-detect from")
    _add_path_args(sp)
    sp.set_defaults(func=_cmd_paths)
