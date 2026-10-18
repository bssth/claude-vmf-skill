# claude-vmf-skill

A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill for working with
Source Engine **VMF** map files (Hammer / Hammer++): inspect, query, validate,
surgically edit and generate geometry — without ever loading a multi-megabyte map
into the model's context.

Real maps are often 5-10 MB and hundreds of thousands of lines. Reading one raw
burns the context window instantly. This skill gives the agent a CLI that returns
summaries and small, exact slices instead, plus edit commands that change only
what was asked for.

It also lets the agent **look at what it built**: draw the map to a PNG, compile
it and get leaks and errors explained, and screenshot the compiled map in the
game from chosen camera positions.

## What's inside

```
SKILL.md              skill entry point: triggers, cheat-sheet, decision tree, gotchas
reference/format.md   VMF format reference (loaded on demand)
scripts/vmf.py        parser / model / serializer / brush geometry + CLI (pure stdlib, Python 3.8+)
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

## Seeing the map (render, compile, shoot)

```bash
python scripts/vmf.py render  maps/mymap.vmf -o plan.png --cut 100        # one floor, roofs removed
python scripts/vmf.py render  maps/mymap.vmf -o side.png --axis y --slice -64 64
python scripts/vmf.py compile maps/mymap.vmf --install                     # vbsp + vvis -fast + vrad -fast
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
