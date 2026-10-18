---
name: vmf
description: Work with Source Engine VMF / Hammer++ map files (.vmf) — inspect, query, validate, surgically edit (keyvalues, I/O connections, materials), generate geometry (brushes, entities, whole greybox blockouts of rooms / corridors / stairs from a JSON layout, holes carved through walls), and SEE the result (render a plan/side view to PNG, compile with vbsp/vvis/vrad and get leaks/errors summarised, screenshot the compiled map in the game). Use whenever a task involves reading, modifying, building, compiling or looking at a .vmf map, Hammer/Hammer++ maps, brushes/solids/sides, entity keyvalues, inputs/outputs (connections), worldspawn, displacements, targetnames, leaks, or level layout. ESSENTIAL for large maps that are too big to read into context.
---

# VMF (Hammer++ map format)

VMF is recursive KeyValues text: `BlockName { "key" "value"  ChildBlock { ... } }`.
Top-level blocks: `versioninfo`, `visgroups`, `viewsettings`, the Hammer++
`*_plus` blocks, `world` (holds `solid`→`side`→`plane`/`uaxis`/`vaxis`/`dispinfo`),
many `entity` blocks (point or brush, with `connections` for I/O and `solid` for
brush ents), then `cameras`/`cordons`. Full spec: [reference/format.md](reference/format.md).

## The one rule that matters most

**Never read a whole map into context.** Real maps run to multiple MB / hundreds
of thousands of lines (a detailed single-player map is easily 5-10 MB). Reading
one raw burns the window instantly. Use `scripts/vmf.py` to get summaries and
surgical slices instead.

## Tooling: `scripts/vmf.py`

Pure-stdlib Python 3.8+ (no dependencies; blockouts, `render` and
`compile`/`shoot` live in the sibling `vmf_build.py` / `vmf_render.py` /
`vmf_game.py` but run through `vmf.py`). Paths below are
relative to this skill's directory; run it from anywhere with an absolute or
relative path to the map (use whichever of `python` / `python3` / `py` works on
the machine). The
parser is **byte-exact**: parse→serialize reproduces the file identically, so
edits change only what you asked for (check any map with `roundtrip`, or a whole
tree with `scripts/test_roundtrip.py`).

### Read / analyze (zero risk)
```
python scripts/vmf.py stats    MAP.vmf [--json]            # counts by classname, brushes, targetnames
python scripts/vmf.py find     MAP.vmf --class npc_* --targetname foo* --kv health=100 [--json] [--limit N]
python scripts/vmf.py get      MAP.vmf --id 14893          # dump ONE subtree (the surgical slice)
python scripts/vmf.py get      MAP.vmf --targetname relay1 # dump matching entities in full
python scripts/vmf.py io-graph MAP.vmf [--targetname foo]  # I/O connections
python scripts/vmf.py refs     MAP.vmf NAME [--json]       # reverse: every entity referencing NAME (keyvalues + I/O)
```
`refs` is the go-to when debugging "who controls/targets this NPC/door?" — it
catches every reference key (`target`, `m_iszEntity`, `goalent`, `parentname`,
`filtername`, `TemplateName`, ...) plus I/O targets, with wildcard matching both
ways, and separates definition sites from references.

### Validate
```
python scripts/vmf.py lint MAP.vmf      # dup ids (INFO); dangling I/O, degenerate planes, invalid brushes (WARN); exit!=0 on WARN
```
`invalid_brushes` = planes that don't bound a volume (reversed winding, open
brush). vbsp drops these **silently**, so a generated or hand-edited brush that
lint flags will simply be missing in game (and often cause a leak).

### Surgical edit — all support `--dry-run` (show diff, write nothing)
Selector for the edit commands: `--class` / `--targetname` / `--kv` (wildcards `* ?`) or `--id` for one node.
```
python scripts/vmf.py set-kv  MAP.vmf KEY VALUE  --targetname foo
python scripts/vmf.py add-io  MAP.vmf --output OnTrigger --target door1 --input Open [--param x --delay 0 --times -1] --targetname btn1
python scripts/vmf.py rm-io   MAP.vmf --output OnTrigger [--target door1] --targetname btn1
python scripts/vmf.py replace-material MAP.vmf OLD/MAT NEW/MAT      # wildcards ok
python scripts/vmf.py move    MAP.vmf DX DY DZ --id 8              # translates geometry; NO texture lock
```

### Generate
```
python scripts/vmf.py new-id      MAP.vmf --count 3                # fresh non-colliding ids
python scripts/vmf.py box         MAP.vmf --mins -64 -64 0 --maxs 64 64 128 --material BRICK/BRICKWALL001 [--into-world]
python scripts/vmf.py add-entity  MAP.vmf --class logic_relay --targetname r1 --origin "0 0 0" --kv StartDisabled=0
```
`box` without `--into-world` prints a `solid` block indented for pasting inside `world`.

### Blockouts: build a playable greybox
```
python scripts/vmf.py build    LAYOUT.json OUT.vmf [--overwrite | --append] [--dry-run]   # whole blockout from JSON
python scripts/vmf.py new-map  OUT.vmf                                                    # empty map (world only)
python scripts/vmf.py room     MAP.vmf --mins 0 0 0 --maxs 512 384 160 [--door "east c"] [--window "north 64"]
                               [--hole "WALL OFFSET W H [SILL]"] [--wall 16 --floor 16 --ceiling 16] [--light 300]
python scripts/vmf.py corridor MAP.vmf --from 512 192 0 --to 768 192 0 --width 96 --height 112 [--via "X Y Z"]
                               [--from-end auto|open|cap] [--to-end ...] [--light 120]
python scripts/vmf.py stairs   MAP.vmf --start 1104 40 0 --dir +y (--steps 22 | --top 176) --width 96
                               [--rise 8 --run 12 --landing 80 --no-clip]
python scripts/vmf.py carve    MAP.vmf --mins X Y Z --maxs X Y Z [--detail]               # hole through existing brushes
```
`room`/`corridor`/`stairs`/`carve` edit MAP in place (all take `--dry-run`, and
`--material` or `--wall-material`/`--floor-material`/`--ceiling-material`).
Everything is axis-aligned boxes from `make_box` (the stair clip ramp is the one
sloped brush), ids from the allocator, and every new brush is checked like
`lint invalid_brushes` before anything is written.

- **Rooms and corridors are given by their INTERIOR** (the air). Floor, ceiling
  and walls are built outside it, so the floor top is the interior min z and a
  room on the next floor starts at `ceiling top` (e.g. interior 0..160, 16 thick
  ceiling, upper interior from 176). Rooms whose interiors are one wall apart
  share a wall position; two walls apart = back-to-back walls.
- **Openings** (`--door/--window/--hole`, JSON `openings`): `WALL OFFSET [WIDTH
  [HEIGHT [SILL]]]`. WALL is north/south/east/west (= +y/-y/+x/-x). OFFSET runs
  from the wall's inside min corner (x for north/south, y for east/west) to the
  opening's near edge; `c` = centred, `@X` (JSON `"at"`) = centred on world
  coordinate X. Door 64x112, window 96x64 with sill 48. An opening also cuts
  through brushes built right behind the wall (a neighbour's back-to-back wall).
- **corridor** points are the floor centre of the passage; each segment runs
  along x or y (`--via` / JSON `points` for bends). An end that runs into or
  touches a wall, or ends inside a known room, is **open** (the wall is cut
  through); otherwise it's **capped**, so a corridor never leaks by itself.
- **stairs** `start` = floor centre of the first step's front edge. Steps are
  solid down to `base` (default start z), rise 8 / run 12 by default (walkable
  up to 18), plus a `tools/toolsplayerclip` ramp through the nosings that starts
  one run in front of the first step. It prints the top end point: the landing
  or upper floor must continue from there at the top z.
- **carve** splits the axis-aligned world boxes it overlaps (`--detail`: also
  func_detail) into the boxes around the hole; each piece keeps the material
  and texture axes of the original face pointing the same way. Sloped brushes
  and displacements are skipped and listed.

**Layout JSON** for `build` (whole-line `//` comments allowed; unknown keys are
warned about, so typos don't pass silently):
```
{
  "world":    {"skyname": "sky_day01_01"},                                   // worldspawn keys
  "defaults": {"wall": 16, "floor": 16, "ceiling": 16, "material": "M",      // or per surface:
               "wall_material": "DEV/DEV_MEASUREWALL01A", "floor_material": "DEV/DEV_MEASUREGENERIC01B",
               "ceiling_material": "DEV/DEV_MEASUREGENERIC01", "stairs_material": "M", "box_material": "M",
               "light": 300, "corridor_light": 120, "light_color": [255, 240, 220], "light_spacing": 320},
  "visgroup": "blockout",                                                     // "" = none
  "rooms": [{"name": "lobby", "mins": [0, 0, 0], "maxs": [512, 384, 160],    // or "size": [w, d, h]
             "openings": [{"wall": "east", "type": "door|window|hole", "offset": 64, "at": 192,
                           "width": 64, "height": 112, "sill": 0}],
             "light": 300, "nodes": 128}],                                    // 0 = no light; info_node grid
  "corridors": [{"name": "hall", "from": "lobby", "to": "stairwell", "width": 96, "height": 112, "at": 192, "z": 0},
                {"points": [[544, 64, 0], [672, 64, 0], [672, 640, 0]], "width": 64, "height": 96,
                 "from_end": "auto|open|cap", "to_end": "auto"}],
  "stairs":   [{"start": [1104, 40, 0], "dir": "+y", "steps": 22, "top": 176, "rise": 8, "run": 12,
                "width": 96, "landing": 80, "clip": true, "base": 0}],
  "boxes":    [{"mins": [768, 304, 160], "maxs": [1056, 384, 176], "material": "M", "entity": "func_detail"}],
  "carve":    [{"mins": [x, y, z], "maxs": [x, y, z]}],                       // e.g. a hole through a floor
  "entities": [{"class": "info_player_start", "origin": [96, 192, 0], "yaw": 0},
               {"class": "npc_citizen", "room": "office", "origin": [64, 0, 0], "look_at": [0, 0, 0], "name": "bob",
                "kv": {"spawnflags": "0"}},
               {"class": "light", "origin": [960, 160, 112], "brightness": 200, "color": [255, 255, 255]}],
  "views":    [{"name": "stairs", "pos": [1104, 8, 64], "look_at": [1104, 384, 200]},   // pos = EYE
               {"room": "lobby", "corner": "sw|se|nw|ne"}]
}
```
- Corridor `from`/`to` are room names (a straight corridor between their facing
  walls, centred on their overlap unless `at`; `z` needed when the floors
  differ) or points. Entity `room` makes `origin` relative to that room's floor
  centre. `look_at` / `yaw` / `pitch` / `angles` set angles (`look_at` only
  turns the yaw, except on info_target / point_*). Views become `info_target`s named
  `shot_<name>`, which `shoot` picks up; a `room` view stands in a corner at eye
  height looking at the room's centre.
- Build order: all room shells, then room openings, corridors, `carve`, stairs,
  boxes, automatic lights (rooms: a grid with one per `light_spacing` cell, a
  third of the way down from the ceiling; corridors: dimmer) and nodes, then
  entities and views. So carves never cut stairs or boxes, and a door between
  back-to-back rooms goes through both walls.
- It prints each room, the corridor end states (open/cap), where each stair run
  ends, and a WARN for every opening or open corridor end that leads to no room
  (that's a leak: put a room or a sealed box behind it).

**The blockout loop**: sketch on the grid first (player 32 wide, 72 tall, eyes
at 64; doors 64x112; corridors >= 64 wide, 96 comfortable; rooms 128-192 high)
→ `build --dry-run` until there are no warnings → `build --overwrite` → `lint` →
`render --cut Z` for each floor and a side view through the stairs and
corridors → `compile` (no leak) → `shoot` (start view + `shot_*` views) → look,
fix the JSON, rebuild. Rebuilding replaces the file, so keep changes in the
JSON, not in Hammer, until the layout settles; `--append` puts a layout into an
existing map once (run it again and you get a second copy).

### See it: render → compile → shoot
You can't judge geometry from coordinates. Look at it, then open the PNG/JPGs
with the Read tool (they are images).
```
python scripts/vmf.py render  MAP.vmf -o plan.png [--cut Z | --z-range Z1 Z2] [--axis z|x|y] [--slice A B]
                              [--bbox X1 Y1 X2 Y2] [--class/--targetname/--kv SEL --focus [MARGIN]]
                              [--leak MAP.lin] [--mark "x y z:label"] [--views views.txt] [--size 1600]
python scripts/vmf.py compile MAP.vmf [--mode fast|normal|final] [--no-vis] [--no-rad] [--hdr] [--install]
python scripts/vmf.py shoot   MAP.vmf [--views views.txt] [--view "name x y z pitch yaw"] [--out DIR]
python scripts/vmf.py game-paths [MAP.vmf]     # which tools / game dir / hl2.exe will be used
```
- **render**: plan view by default (`--axis x|y` = side views). Brushes are filled
  by kind (world, nodraw, detail, brush entity, displacement, water) and shaded by
  height/depth; triggers hatched, clips dashed, sky/areaportals outlined, invalid
  brushes red. Entities: player start (green, with facing arrow), NPCs (red,
  arrow), lights, props, items, logic, info_node dots, `shot_*` cameras. A
  labelled grid plus the printed `pixel->world` formula let you read coordinates
  back. A plain top view shows **roofs**: use `--cut Z` (drop everything above Z)
  or `--z-range` to see one floor. The 3D skybox is left out of the frame
  automatically. Geometry comes from the planes (what vbsp compiles);
  `--editor-verts` uses Hammer++'s `vertices_plus` instead (faster on huge maps).
- **compile**: runs vbsp → vvis → vrad from the game's `bin`, prints per-tool
  status, errors/warnings grouped by kind with counts, the fullest BSP limits, and
  on a leak: the entity, the `.lin` pointfile and the `render --leak` command to
  look at it (vvis is skipped on a leak). `fast` (default) is for iteration;
  `final` = full vis, `vrad -final`, LDR+HDR. Exit code != 0 on a leak, a failed
  tool, or a limit at 100%. `--install` copies the bsp into `<game>/maps` (it never
  replaces a map it didn't put there unless `--overwrite`). Full output: the
  tools' own `MAP.log` next to the map.
- **shoot**: starts the game windowed on the compiled map, freezes AI and input,
  hides HUD/viewmodel, moves a noclip camera to each view and saves one JPG per
  view to `--out` (default `./shots_<map>`). Each line reports the camera's real
  eye position/angles (read back with `getpos`) and the shot is retaken if the
  camera drifted. Views are EYE positions + Source angles (pitch down-positive,
  yaw CCW from +X): a file of `name x y z pitch yaw [roll]` lines, `--view`, or
  automatic: player start looking 4 ways + every `point_viewcontrol` + every
  entity named `shot_*` (drop an `info_target` named `shot_<what>` with `angles`
  into the map to add a view). Needs Steam running and the game closed; takes
  ~20 s to load plus ~1 s per view. Restores `cfg/config.cfg` afterwards.
  `--fullbright` for maps compiled with `--no-rad`.
- Paths are auto-detected (game dir = nearest folder above the map with
  `gameinfo.txt`, tools = `<game dir>/../bin`, `hl2.exe` next to it or via the
  Steam library). For maps outside the game tree set them once in
  `vmf.local.json` next to this file (gitignored):
  `{"game_dir": "...", "bin_dir": "...", "game_exe": "..."}`, or env
  `VMF_GAME_DIR` / `VMF_BIN_DIR` / `VMF_GAME_EXE`, or flags.

## Decision tree (hybrid: tool vs manual Edit)

- Reading, counting, searching, I/O graph → **always vmf.py** (never raw Read).
- Validation before a vbsp compile → **vmf.py lint**.
- Generating geometry/entities → **vmf.py** (computing planes/uaxis by hand silently breaks convexity & normals).
- A new space / level / greybox from a description → **a layout JSON + `build`**
  into a new map, then the blockout loop. One room, corridor or stair run added
  to an existing map → **`room` / `corridor` / `stairs`** with `--dry-run` first.
- A door, window or stairwell hole through existing walls / floors → **`carve`**
  (axis-aligned brushes only; it lists what it couldn't cut).
- Changing a keyvalue / I/O on entities → **vmf.py set-kv / add-io / rm-io** (safe with repeated keys; preview with `--dry-run`).
- A one-off change to a single, textually-unique line you already located → a manual **Edit** is fine.
- **After building or moving geometry → render and look** (plan with `--cut`, a
  side view through anything vertical). Fix what looks wrong before compiling.
- **After compiling → if it leaked, `render --leak`; otherwise shoot and look.**
  Judge the screenshots like a player would (scale, lighting, readability), not
  just "it compiled".
- Work on a **copy** when experimenting: compile writes `.bsp/.prt/.lin/.log`
  next to the map, and the user's own maps may have uncommitted Hammer work.
  `shoot` installs the bsp into `<game>/maps`: delete it there when you're done
  with a throwaway map.

## Gotchas

- Hammer++ writes **LF** line endings, even when the surrounding repo is CRLF. The tool detects & preserves line endings per file — don't normalize them.
- Keep any script output you add **ASCII**: Windows consoles with a legacy code page (e.g. cp1251, cp1252) crash on non-ASCII prints.
- Old Valve-source VMFs can be cp1252, not UTF-8: the tool decodes UTF-8 first, else latin-1, and writes the file back in the same encoding (byte-exact). Non-ASCII map text prints as `\xNN` escapes; an edit that latin-1 can't hold (e.g. `set-kv` with a non-Latin letter) aborts without writing.
- `id`s are shared across solids/sides/entities and must be unique; always allocate with `new-id` / the tool's allocator, never hand-pick.
- Keys repeat (`vertices_plus` `"v"`, multiple same-named I/O outputs). Never model VMF as a plain dict.
- Hammer++ `*_plus` blocks must be preserved — the tool does this automatically.
- `move` does not apply texture lock; textures shift relative to moved geometry (acceptable for most edits, re-align if it matters).
- Plane winding: vbsp's outward normal is `(p0-p1) x (p2-p1)`, so `(p1-p0) x (p2-p0)` points INTO the brush. Get it backwards and the brush silently vanishes from the compile. Details in [reference/format.md](reference/format.md).
- `hidden { ... }` blocks (objects hidden with H in Hammer) are not compiled by vbsp; the tools skip them too.
- An opening in an outer wall with nothing behind it (a window onto the void) leaks. `build` warns about each one; `room` can't know what's outside, so check with `compile`.
- `render --slice A B` also draws brushes that just touch A or B: to look through a corridor or stair run from the side, inset the slice a few units (`--slice 1064 1144` for stairs spanning 1056..1152).
- `shoot` drives the game with `hl2.exe -hijack` (console commands sent to the running instance) and `-condebug` (`console.log` in the game dir). If someone is using the machine, the game window grabs focus when it starts; input is zeroed during the shots, so that's harmless.

## After editing

Re-run `python scripts/vmf.py lint MAP.vmf` and, for sanity, `roundtrip` on the
result. If the map is under version control, diff it to confirm only the intended
lines changed. For geometry or layout changes, `render` it and look; before
calling a level change done, `compile` + `shoot` and look at the shots.
