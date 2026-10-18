---
name: vmf
description: Work with Source Engine VMF / Hammer++ map files (.vmf) — inspect, query, validate, surgically edit (keyvalues, I/O connections, materials), generate geometry (brushes, entities, whole greybox blockouts of rooms / corridors / stairs from a JSON layout, holes carved through walls, and non-box shapes - rotated boxes, wedges/ramps, cylinders/pipes, cones, rings/tunnels, arches, gable/shed/barrel roofs, polyline walls and fences, displacement terrain with hills), check that materials/models exist in the game's VPKs, and SEE the result (render a plan/side view to PNG, compile with vbsp/vvis/vrad and get leaks/errors summarised, screenshot the compiled map in the game). Use whenever a task involves reading, modifying, building, compiling or looking at a .vmf map, Hammer/Hammer++ maps, brushes/solids/sides, entity keyvalues, inputs/outputs (connections), worldspawn, displacements, targetnames, leaks, or level layout. ESSENTIAL for large maps that are too big to read into context.
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

Pure-stdlib Python 3.8+ (no dependencies; blockouts, shapes, `render`,
`compile`/`shoot` and `assets` live in the sibling `vmf_build.py` /
`vmf_shapes.py` / `vmf_render.py` / `vmf_game.py` / `vmf_assets.py` but run
through `vmf.py`). Paths below are
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
                               [--roof gable|shed|barrel|flat --roof-axis x|y --roof-rise 96]
python scripts/vmf.py corridor MAP.vmf --from 512 192 0 --to 768 192 0 --width 96 --height 112 [--via "X Y Z"]
                               [--from-end auto|open|cap] [--to-end ...] [--light 120]
python scripts/vmf.py stairs   MAP.vmf --start 1104 40 0 --dir +y (--steps 22 | --top 176) --width 96
                               [--rise 8 --run 12 --landing 80 --no-clip]
python scripts/vmf.py carve    MAP.vmf --mins X Y Z --maxs X Y Z [--detail]               # hole through existing brushes
```
`room`/`corridor`/`stairs`/`carve` edit MAP in place (all take `--dry-run`, and
`--material` or `--wall-material`/`--floor-material`/`--ceiling-material`).
Rooms, corridors and stairs are axis-aligned boxes from `make_box` (the stair
clip ramp and room roofs are the sloped exceptions; anything else non-boxy is a
shape, below), ids from the allocator, and every new brush is checked like
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
                           "width": 64, "height": 112, "sill": 0,
                           "repeat": {"count": 5, "step": 96}}],             // a row of windows
             "roof": {"style": "gable", "axis": "y", "rise": 96},             // optional, see Shapes 'roof'
             "light": 300, "nodes": 128}],                                    // 0 = no light; info_node grid
  "corridors": [{"name": "hall", "from": "lobby", "to": "stairwell", "width": 96, "height": 112, "at": 192, "z": 0},
                {"points": [[544, 64, 0], [672, 64, 0], [672, 640, 0]], "width": 64, "height": 96,
                 "from_end": "auto|open|cap", "to_end": "auto"}],
  "stairs":   [{"start": [1104, 40, 0], "dir": "+y", "steps": 22, "top": 176, "rise": 8, "run": 12,
                "width": 96, "landing": 80, "clip": true, "base": 0}],
  "boxes":    [{"mins": [768, 304, 160], "maxs": [1056, 384, 176], "material": "M", "entity": "func_detail",
                "repeat": {"count": [3, 2], "step": [128, 96]}}],             // count N + step [dx,dy,dz], or a grid
  "carve":    [{"mins": [x, y, z], "maxs": [x, y, z]}],                       // e.g. a hole through a floor
  "shapes":   [{"type": "cylinder", "base": [0, 0, 0], "height": 256, "radius": 16, "detail": true},
               {"type": "terrain", "mins": [-4096, -4096], "maxs": [4096, 4096], ...}],   // see Shapes below
  "entities": [{"class": "info_player_start", "origin": [96, 192, 0], "yaw": 0},
               {"class": "npc_citizen", "room": "office", "origin": [64, 0, 0], "look_at": [0, 0, 0], "name": "bob",
                "kv": {"spawnflags": "0"}},
               {"class": "light", "origin": [960, 160, 112], "brightness": 200, "color": [255, 255, 255],
                "repeat": {"count": 4, "step": [256, 0, 0]}},
               {"class": "logic_relay", "name": "r1", "origin": [0, 0, 64],
                "outputs": [["OnTrigger", "door1", "Open", "", 0.5, -1]]},   // output, target, input, param, delay, times
               {"class": "prop_static", "models": ["models/a.mdl", "models/b.mdl"], "yaw": "random",
                "on_terrain": true, "origin": [0, 0, -8],                      // z = terrain height + origin z
                "scatter": {"count": 80, "mins": [x, y], "maxs": [x, y],      // or "polygon": [[x, y], ...]
                            "avoid": [{"mins": [x, y], "maxs": [x, y]}], "min_dist": 200, "seed": 1}}],
  "views":    [{"name": "stairs", "pos": [1104, 8, 64], "look_at": [1104, 384, 200]},   // pos = EYE
               {"room": "lobby", "corner": "sw|se|nw|ne"}],
  "prefabs":  {"target": {"boxes": [...], "shapes": [...], "entities": [...]}},  // local coords, origin 0 0 0
  "place":    [{"prefab": "target", "name": "t{i}", "origin": [x, y, z], "yaw": 90,
                "vars": {"flags": 578}, "repeat": {"count": 9, "step": [112, 0, 0]}}]
}
```
- Corridor `from`/`to` are room names (a straight corridor between their facing
  walls, centred on their overlap unless `at`; `z` needed when the floors
  differ) or points. Entity `room` makes `origin` relative to that room's floor
  centre. `look_at` / `yaw` / `pitch` / `angles` set angles (`look_at` only
  turns the yaw, except on info_target / point_*). Views become `info_target`s named
  `shot_<name>`, which `shoot` picks up; a `room` view stands in a corner at eye
  height looking at the room's centre.
- Build order: all room shells (and their roofs), then room openings, corridors,
  `carve`, stairs, boxes, shapes, automatic lights (rooms: a grid with one per
  `light_spacing` cell, a third of the way down from the ceiling; corridors:
  dimmer) and nodes, then entities and views. So carves never cut stairs, boxes
  or shapes, and a door between back-to-back rooms goes through both walls.
- **I/O**: `outputs` on entities, entity boxes and entity shapes: each is
  `[output, target, input, param, delay, times]` (the last three optional) or a
  dict with those keys; written like Hammer++ (`,`-joined; ESC when a field has
  a comma). World brushes can't have outputs. `lint` then checks the targets.
- **Brush entities**: `entity` + `kv` + `name` on a box or shape; `origin` sets
  the entity origin, which is the hinge / axis of `func_door_rotating`,
  `func_rot_button`, `func_rotating`. `trigger_*` get `TOOLS/TOOLSTRIGGER` and
  spawnflags 1 (clients) unless given.
- **Prefabs**: a named group of `boxes` / `shapes` / `entities` in local
  coordinates, stamped by `place` entries: moved to `origin`, turned by `yaw`
  about it (boxes become rotatable box shapes, entity origins and angles turn
  too), and every string gets `{name}` (the placement's name), `{i}` (its index
  from 1 across `repeat`) and `vars` substituted, so targetnames and I/O stay
  unique per copy. Use them for anything repeated with logic (pop-up targets,
  lamp posts with lights, doors with triggers). Axis-dependent keyvalues (e.g.
  a rotating entity's axis spawnflag) don't turn with `yaw`: pass them per
  placement through `vars`.
- `repeat` works on boxes, shapes, entities, room openings and placements. `models` picks
  one model per placement (deterministic). `scatter` places an entity at random
  but reproducible points (`seed`); `on_terrain` drops it onto a terrain shape
  built in the same layout.
- A room `roof` sits on the walls (or on the ceiling slab if `ceiling` > 0);
  with `"ceiling": 0` the roof alone closes the room, and its gable-end infill
  takes the walls' thickness and material. `roof_material` is a `defaults` key.
- An **outdoor map** is a big room with `TOOLS/TOOLSSKYBOX` walls and ceiling,
  `TOOLS/TOOLSNODRAW` floor and `"light": 0`, a `terrain` shape filling its floor
  (its `thickness` reaching down to the room's floor), buildings as rooms inside
  it (`"floor": 4` puts a 4-unit slab on the ground), and `light_environment` /
  `env_sun` / `shadow_control` / `env_fog_controller` in `entities`. Openings
  that face the outdoor room don't warn: it is an interior too.
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

### Shapes: brushes that aren't boxes
```
python scripts/vmf.py shape MAP.vmf '{"type": "arch", ...}' ['{...}' ...] [--dry-run]   # or @specs.json (a list)
```
The same spec objects go in a layout's `"shapes"` list. vbsp only takes convex
brushes, so every shape is cut into convex pieces that share faces exactly
(each face plane comes from three of its vertices chosen from the point set, so
neighbours get identical opposite planes: no cracks). Vertices are rounded to
1/1000 (`"snap": 1` snaps to whole units); `vertices_plus` is computed from the
final planes.

| type | keys (besides the common ones) |
|---|---|
| `box` | `mins`+`maxs` or `center`+`size`; rotate with `yaw`/`pitch`/`roll` |
| `prism` | `points` (convex footprint [[x,y],...]), `z` |
| `wedge` | `mins`, `maxs`, `up` (+x/-x/+y/-y: the side that is high), `low` (height kept at the low end); reports the slope (walkable up to ~45 deg) |
| `cylinder` | `base`+`height` (vertical) or `from`+`to` (any direction: pipes, logs, bars), `radius`, `sides` (auto 8..32), `align` flat/point |
| `cone` | like cylinder + `top_radius` (0 = pointed; else a frustum) |
| `ring` | like cylinder (or `center`+`axis`+`length`) + `radius`, `inner` or `thickness`, `arc` [a0, a1] deg: curved walls, tyres, tunnels/barrel vaults (horizontal axis: 0 = horizontal, 90 = up) |
| `arch` | wall from `from` to `to` ([x,y]), `z` [z0,z1], `thickness`, `align` center/left/right, `openings`: [{`at` (distance along the wall), `width`, `height` (to the crown), `rise` (default = half width: round arch; less = segmental), `sill`}], `segments` |
| `roof` | `style` gable/shed/barrel/flat, footprint `mins`/`maxs` [x,y] (outside of the walls), eave `z`, `axis` x/y (ridge direction), `rise` or `pitch` (deg), `up` (shed: the high side), `overhang`, `end_overhang`, `thickness`, `gables` (end-wall infill: true / {thickness, material} / false), `segments` (barrel) |
| `wall` | `points` [[x,y],...], `z`, `thickness`, `closed`, `align` center/left/right, `gaps` [[d0,d1],...] (distances along the polyline: doorways); mitered corners |
| `fence` | a `wall` preset: thickness 4, `METAL/METALFENCE001A` both sides, nodraw top/bottom, func_detail, `posts` spacing (default 128), `post_size`, `post_material` |
| `railing` | `points`, `z` (the floor it stands on), `height` (40), `rails` (2), `rail_size`, `posts` spacing (64), `post_size`, `closed`, `gaps`; see-through, func_detail |
| `terrain` | `mins`/`maxs` [x,y] (whole `tile`s, default 512), `z`, `power` 2-4, `thickness`, `material` (a blend material for alpha), `flat_material`, `hills` [{`at`,`radius`,`height`,`shape` smooth/cone/gauss}], `ridges` [{`points`,`width`,`height`}], `noise` {`amp`,`scale`,`seed`,`octaves`}, `edge` {`height`,`width`} (a rim rising toward the rectangle's sides: hides the sky walls), `flat` [{`mins`,`maxs` or `polygon`, `blend`}] (height forced to 0 inside, blended outside), `clamp`, `alpha` {`from`,`to`} (paint layer 2 where the slope's normal z goes from..to), `skip_flat` (default true: flat tiles become plain brushes) |

Common keys: `material`, `materials` (by face tag: `top`/`bottom`/`side`, and
`outer`/`inner`/`end` on rings), `entity` (e.g. `func_detail`,
`func_breakable`) + `kv` + `name`, `detail: true` (= func_detail), `snap`,
`yaw`/`pitch`/`roll` + `pivot` (rotation, default about the bbox centre),
`repeat`, `tex_scale`, `lightmapscale`. Textures are world-aligned, projected
into sloped faces.

- **Displacements don't seal the map** and don't block vis (vbsp drops a
  displacement's base brush entirely): keep the sky room's floor (or other
  brushes) under a terrain.
- A `fast` compile shows **dark seams along terrain tile edges** (vrad -fast
  lighting); `--mode normal` / `final` lights them smoothly. Not a geometry
  problem: don't "fix" the heights.
- Terrain heights are computed from world x/y, so tile edges always match; the
  grid follows vbsp's own face winding (rows from the start corner along the
  face's second vertex), checked against the compiled BSP.
- `fence`'s default material is `$translucent` with `%compilepassbullets`
  (bullets go through it) and the fence lives in func_detail, so it doesn't cut
  vis or seal anything either.

### Assets: does the game have it?
```
python scripts/vmf.py assets 'materials/metal/*fence*.vmt' 'models/props_foliage/*pine*.mdl' [--where] [--limit N]
python scripts/vmf.py assets --color 'materials/wood/woodwall*.vmt'   # + each material's average colour
python scripts/vmf.py assets --missing MAP.vmf      # materials / models / sky / vscripts / soundscapes the game can't find (exit 1 if any)
python scripts/vmf.py assets --paths                # the gameinfo.txt search paths used
```
Reads the mod's `gameinfo.txt` `game` search paths, loose folders and VPK
directories (no extraction), so it sees exactly what the engine mounts.
`--color` reads the average colour stored in each base texture's VTF header
(HL2's `DEV/DEV_MEASUREWALL01A/B/C`, `GENERIC01`, `CRATE*` are all orange;
`GENERIC01B` and `WALL01D` are grey): pick a greybox palette whose parts read
apart. Pick materials and models with it **before** writing them into a layout, and run
`--missing` before compiling: a wrong name is a purple checkerboard or an error
model in game, and vbsp deletes `prop_static`s whose model can't be static
(e.g. `models/props_vehicles/apc001.mdl` needs `prop_dynamic`).

### See it: render → compile → shoot
You can't judge geometry from coordinates. Look at it, then open the PNG/JPGs
with the Read tool (they are images).
```
python scripts/vmf.py render  MAP.vmf -o plan.png [--cut Z | --z-range Z1 Z2] [--axis z|x|y] [--slice A B]
                              [--bbox X1 Y1 X2 Y2] [--class/--targetname/--kv SEL --focus [MARGIN]]
                              [--leak MAP.lin] [--mark "x y z:label"] [--views views.txt] [--size 1600]
python scripts/vmf.py compile MAP.vmf [--mode fast|normal|final] [--vis fast|full] [--no-vis] [--no-rad] [--hdr] [--install]
python scripts/vmf.py shoot   MAP.vmf [--views views.txt] [--view "name x y z pitch yaw [| cmd; cmd]"] [--out DIR]
                              [--exec "console cmd"] [--hud] [--cmd-wait 1]
python scripts/vmf.py game-paths [MAP.vmf]     # which tools / game dir / hl2.exe will be used
```
- **render**: plan view by default (`--axis x|y` = side views). Brushes are filled
  by kind (world, nodraw, detail, brush entity, displacement, water) and shaded by
  height/depth; triggers hatched, clips dashed, sky/areaportals outlined, invalid
  brushes red. Entities: player start (green, with facing arrow), NPCs (red,
  arrow), lights, props, items, logic, info_node dots, `shot_*` cameras;
  displacements are drawn as their real displaced surface, cell by cell, shaded
  by height (a side view shows the terrain profile). A
  labelled grid plus the printed `pixel->world` formula let you read coordinates
  back. A plain top view shows **roofs**: use `--cut Z` (drop everything above Z)
  or `--z-range` to see one floor. The 3D skybox is left out of the frame
  automatically. Geometry comes from the planes (what vbsp compiles);
  `--editor-verts` uses Hammer++'s `vertices_plus` instead (faster on huge maps).
- **compile**: runs vbsp → vvis → vrad from the game's `bin`, prints per-tool
  status, errors/warnings grouped by kind with counts, the fullest BSP limits, and
  on a leak: the entity, the `.lin` pointfile and the `render --leak` command to
  look at it (vvis is skipped on a leak). `fast` (default) is for iteration;
  `final` = full vis, `vrad -final`, LDR+HDR; `--vis fast` keeps vvis quick in
  any mode (a full vis of a big open outdoor map can take tens of minutes, and
  screenshots only need the lighting). Exit code != 0 on a leak, a failed
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
  into the map to add a view). **Testing logic**: text after `|` in a view runs
  as console commands after the camera is placed and before the shot (waits
  `--cmd-wait` s more): `"t_fall 0 900 80 10 90 | ent_fire range_n1 press"`;
  `--exec` runs commands once after load; `--hud` keeps game_text / HUD visible;
  script `printl` output lands in `<game>/console.log` (appended across runs:
  read from the last `Redownloading all lightmaps`). Quote console arguments
  that contain `( ) ' :` (`ent_fire s runscriptcode "Go()"`): the console
  tokenizer splits on them. Outputs a command triggers run a tick later than
  the next `ent_fire` in the same view: check their results in the next view.
  Needs Steam running and the game closed; takes
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
- Anything round, sloped, angled or curved (ramps, columns, pipes, arches,
  roofs, angled walls, fences, hills) → **a `shapes` entry / `shape`**, never
  hand-computed planes.
- Choosing a material or model → **`assets PATTERN`** first; after building →
  **`assets --missing`**.
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
