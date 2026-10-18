# claude-vmf-skill

A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill for working with
Source Engine **VMF** map files (Hammer / Hammer++): inspect, query, validate,
surgically edit and generate geometry — without ever loading a multi-megabyte map
into the model's context.

Real maps are often 5-10 MB and hundreds of thousands of lines. Reading one raw
burns the context window instantly. This skill gives the agent a CLI that returns
summaries and small, exact slices instead, plus edit commands that change only
what was asked for.

It can **build playable greybox blockouts**: rooms with doors and windows,
corridors, stairs and holes cut through existing walls, or a whole multi-floor
layout from one short JSON file. Beyond boxes it makes **shapes**: rotated
boxes, ramps, cylinders and pipes, cones, rings and tunnels, arched walls,
gable / shed / barrel roofs, polyline walls, fences and railings, and
**displacement terrain** with hills, ridges, noise and a rim that hides the sky
walls; trees and props can be scattered over it.

It also lets the agent **look at what it built**: draw the map to a PNG, compile
it and get leaks and errors explained, and screenshot the compiled map in the
game from chosen camera positions.

## What's inside

```
SKILL.md              skill entry point: triggers, cheat-sheet, decision tree, gotchas
reference/format.md   VMF format reference (loaded on demand)
scripts/vmf.py        parser / model / serializer / brush geometry + CLI (pure stdlib, Python 3.8+)
scripts/vmf_build.py  `build`, `room`, `corridor`, `stairs`, `carve`, `new-map`: blockouts
scripts/vmf_shapes.py `shape`: non-box brushes (convex pieces) and displacement terrain
scripts/vmf_assets.py `assets`: materials / models in the game's search paths and VPKs
scripts/vmf_render.py `render`: plan / side view PNG (stdlib PNG encoder, bitmap font)
scripts/vmf_game.py   `compile`, `shoot`, `game-paths`: vbsp/vvis/vrad and the game
scripts/test_roundtrip.py   byte-exact parse->serialize check over a tree of maps
```

`vmf.py` commands:

| Kind | Commands |
|---|---|
| Read | `stats`, `find`, `get`, `io-graph`, `refs` |
| Validate | `lint`, `roundtrip` |
| Edit (all support `--dry-run`) | `set-kv`, `add-io`, `rm-io`, `replace-material`, `move` |
| Generate | `new-id`, `box`, `add-entity` |
| Blockout | `build`, `new-map`, `room`, `corridor`, `stairs`, `carve`, `shape` |
| Assets | `assets` (find files, `--color`, `--missing MAP`) |
| See | `render`, `compile`, `shoot`, `game-paths` |

Key properties:

- **Byte-exact round-trip.** Parsing and re-serializing an unchanged map reproduces
  it byte for byte (line endings and text encoding included: UTF-8, or latin-1 for
  old cp1252 sources), so a diff after an edit shows only the edit.
- **Repeated keys and interleaving preserved.** VMF is not a dict: `vertices_plus`
  has many `"v"`, `connections` repeats output names, and keyvalues interleave
  with child blocks. The model is an ordered list.
- **Hammer++ `*_plus` blocks preserved** verbatim.
- **Safe writes.** Every edit re-parses its own output before writing and aborts
  if the result would be invalid.

## Install

Clone it as a skill directory named `vmf`, either per project or for your user:

```bash
# per project (commit it with the project, e.g. as a submodule)
git submodule add https://github.com/bssth/claude-vmf-skill.git .claude/skills/vmf

# or for all your projects
git clone https://github.com/bssth/claude-vmf-skill.git ~/.claude/skills/vmf
```

Claude Code picks it up automatically whenever a task involves `.vmf` files, Hammer
maps, brushes, entity keyvalues or I/O connections.

## Using the CLI directly

The script is a standalone tool too:

```bash
python scripts/vmf.py stats maps/mymap.vmf
python scripts/vmf.py find maps/mymap.vmf --class "npc_*" --limit 20
python scripts/vmf.py refs maps/mymap.vmf door_main
python scripts/vmf.py lint maps/mymap.vmf
python scripts/vmf.py set-kv maps/mymap.vmf health 200 --targetname "crate_*" --dry-run
```

Before trusting edits on your own maps, check that they round-trip:

```bash
python scripts/test_roundtrip.py "maps/**/*.vmf"
```

## Blockouts (greybox from a description)

Describe the spaces in a JSON layout and generate the map from it:

```json
{
  "rooms": [
    {"name": "lobby", "mins": [0, 0, 0], "maxs": [512, 384, 160]},
    {"name": "office", "mins": [0, 0, 176], "maxs": [512, 384, 336]},
    {"name": "stairwell", "mins": [768, 0, 0], "maxs": [1152, 384, 352]}
  ],
  "corridors": [
    {"from": "lobby", "to": "stairwell", "width": 96, "height": 112},
    {"from": "office", "to": "stairwell", "width": 64, "height": 112, "at": 344, "z": 176}
  ],
  "stairs": [{"start": [1104, 40, 0], "dir": "+y", "top": 176, "width": 96, "landing": 80}],
  "boxes": [{"mins": [768, 304, 160], "maxs": [1056, 384, 176]}],
  "entities": [{"class": "info_player_start", "origin": [96, 192, 0], "yaw": 0}],
  "views": [{"room": "stairwell", "corner": "sw"},
            {"name": "stairs_up", "pos": [1104, 8, 64], "look_at": [1104, 384, 200]}]
}
```

```bash
python scripts/vmf.py build layout.json maps/blockout.vmf --dry-run   # check: warnings, corridor ends
python scripts/vmf.py build layout.json maps/blockout.vmf --overwrite
python scripts/vmf.py lint  maps/blockout.vmf
```

Rooms and corridors are given by their interior; floors, ceilings and walls are
built around it, openings are cut by splitting the wall into the boxes around
the hole, corridors open into the walls they reach and are capped elsewhere,
stairs get a player-clip ramp, and rooms and corridors get ceiling lights.
`build` warns about every opening that leads to the void (a leak) and about
unknown keys. Room, corridor and stair brushes are axis-aligned boxes from the
same generator as `box`; every brush is checked like `lint invalid_brushes`
before anything is written.
The same pieces work one at a time on an existing map:

```bash
python scripts/vmf.py room     maps/mymap.vmf --mins 0 0 0 --maxs 512 384 160 --door "east c" --window "north 64"
python scripts/vmf.py corridor maps/mymap.vmf --from 512 192 0 --to 768 192 0 --width 96 --height 112
python scripts/vmf.py stairs   maps/mymap.vmf --start 900 64 0 --dir +y --steps 10 --width 64 --landing 32
python scripts/vmf.py carve    maps/mymap.vmf --mins 96 96 -16 --maxs 192 192 0 --dry-run
```

The full layout format (openings, bent corridors, room-relative entities,
camera views for `shoot`, defaults and materials, `repeat`, `scatter`) is in
[SKILL.md](SKILL.md).

## Shapes and terrain

Anything that isn't an axis-aligned box goes in the layout's `"shapes"` list
or through `shape` on an existing map:

```bash
python scripts/vmf.py shape maps/mymap.vmf   '{"type": "arch", "from": [0, 0], "to": [512, 0], "z": [0, 256], "openings": [{"at": 128, "width": 96, "height": 160}]}'   '{"type": "ring", "from": [0, 300, 0], "to": [0, 556, 0], "radius": 48, "thickness": 8, "arc": [0, 180]}'   '{"type": "roof", "style": "gable", "mins": [0, 600], "maxs": [512, 900], "z": 160, "axis": "x", "rise": 96}'
```

Each shape is cut into convex brushes whose shared faces get exactly the same
planes (chosen from the face's vertex set, independent of order), so pieces
meet without cracks; `vertices_plus` is computed from the final planes. Rooms
take a `"roof"` directly. `terrain` builds displacement tiles whose heights come
from world coordinates (hills, ridges, value noise, a raised rim, flattened
zones), with the vertex grid laid out the way vbsp reads it (verified against
compiled BSPs); `"on_terrain": true` drops scattered props onto it.

## Logic: I/O, triggers, prefabs

Entities, entity boxes and entity shapes take `outputs`
(`[output, target, input, param, delay, times]`), brush entities take an
`origin` (the hinge of doors and rotating buttons), and `trigger_*` brushes get
the trigger material by themselves. `"rest": true` stands a prop on its origin
z using the model's bounding box from the .mdl header (most HL2 props have the
origin in their middle, so they'd sink into the floor otherwise). Props whose
model the engine would silently drop on the given class (a physics model on a
prop_static or prop_dynamic, a non-`$staticprop` model on a prop_static) are
moved to the class that keeps them, usually `prop_dynamic_override`. Repeated gameplay pieces are **prefabs**:
a group of boxes / shapes / entities defined once in local coordinates and
stamped with `place` entries (position, yaw, repeat), with `{name}`, `{i}` and
custom `vars` filled into targetnames and I/O so every copy is wired to itself.

```json
"prefabs": {"popup": {
  "shapes": [{"type": "box", "mins": [-20, -2, 2], "maxs": [20, 2, 66], "entity": "func_rot_button",
              "name": "{name}", "origin": [0, 0, 2],
              "kv": {"spawnflags": "578", "distance": "90", "speed": "360", "wait": "3", "health": "1"},
              "outputs": [["OnPressed", "script", "RunScriptCode", "Hit({i})"]]}]}},
"place": [{"prefab": "popup", "name": "t{i}", "origin": [0, 512, 0], "repeat": {"count": 9, "step": [112, 0, 0]}}]
```

`shoot` can test logic: `--view "name x y z pitch yaw | ent_fire t1 press"`
runs console commands before that view's screenshot, and `--hud` keeps
game_text messages on screen.

## Assets

```bash
python scripts/vmf.py assets 'materials/metal/*fence*.vmt' 'models/props_foliage/*pine*.mdl'
python scripts/vmf.py assets --color 'materials/dev/*.vmt'     # average colour per material
python scripts/vmf.py assets --size 'models/props_junk/*.mdl'   # model bounding boxes (for placement)
python scripts/vmf.py assets --missing maps/mymap.vmf           # materials, models, sky, vscripts, soundscapes the game can't find
```

Reads the `game` search paths of the mod's `gameinfo.txt` and the VPK
directories directly, so you can pick materials and models that exist (and see
their colours) before a compile turns typos into purple checkerboards.

## Seeing the map (render, compile, shoot)

```bash
python scripts/vmf.py render  maps/mymap.vmf -o plan.png --cut 100        # one floor, roofs removed
python scripts/vmf.py render  maps/mymap.vmf -o side.png --axis y --slice -64 64
python scripts/vmf.py compile maps/mymap.vmf --install                     # vbsp + vvis -fast + vrad -fast
python scripts/vmf.py compile maps/mymap.vmf --mode normal --vis fast      # real lighting, quick vis (big outdoor maps)
python scripts/vmf.py render  maps/mymap.vmf --leak maps/mymap.lin --focus # after a leak
python scripts/vmf.py shoot   maps/mymap.vmf --out shots/                  # screenshots from the game
```

- `render` needs nothing but Python. Brushes are computed from their planes the
  way vbsp sees them; ones that don't bound a volume are drawn red (`lint` reports
  them too: vbsp drops them silently).
- `compile` and `shoot` target Source SDK 2013-style games on Windows (`vbsp.exe`,
  `vvis.exe`, `vrad.exe`, `hl2.exe`). Paths are auto-detected from the map's
  location (the nearest folder with `gameinfo.txt`, its `../bin`, and `hl2.exe`
  next to it or through the Steam library). Otherwise put them in
  `vmf.local.json` next to `SKILL.md` (gitignored):

  ```json
  {"game_dir": "C:/path/to/mymod", "bin_dir": "C:/path/to/bin", "game_exe": "C:/path/to/hl2.exe"}
  ```

  or set `VMF_GAME_DIR`, `VMF_BIN_DIR`, `VMF_GAME_EXE`, or pass `--game-dir`,
  `--bin-dir`, `--game-exe`. `game-paths` shows what will be used.
- `shoot` starts the game windowed, sends console commands to it with
  `hl2.exe -hijack` (what Hammer uses), takes one JPG per view and verifies the
  camera position with `getpos`. It restores `cfg/config.cfg` afterwards and
  never replaces a map in `<game>/maps` it didn't install itself (unless
  `--overwrite`). Steam must be running and the game must not be.

## License

MIT, see [LICENSE](LICENSE).
