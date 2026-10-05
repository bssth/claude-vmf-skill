---
name: vmf
description: Work with Source Engine VMF / Hammer++ map files (.vmf) — inspect, query, validate, surgically edit (keyvalues, I/O connections, materials), and generate geometry (brushes, entities). Use whenever a task involves reading or modifying a .vmf map, Hammer/Hammer++ maps, brushes/solids/sides, entity keyvalues, inputs/outputs (connections), worldspawn, displacements, or targetnames. ESSENTIAL for large maps that are too big to read into context.
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

Pure-stdlib Python 3.8+ (no dependencies). Paths below are relative to this
skill's directory; run it from anywhere with an absolute or relative path to the
map (use whichever of `python` / `python3` / `py` works on the machine). The
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
python scripts/vmf.py lint MAP.vmf      # dup ids (INFO), dangling I/O + degenerate planes (WARN); exit!=0 on WARN
```

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

## Decision tree (hybrid: tool vs manual Edit)

- Reading, counting, searching, I/O graph → **always vmf.py** (never raw Read).
- Validation before a vbsp compile → **vmf.py lint**.
- Generating geometry/entities → **vmf.py** (computing planes/uaxis by hand silently breaks convexity & normals).
- Changing a keyvalue / I/O on entities → **vmf.py set-kv / add-io / rm-io** (safe with repeated keys; preview with `--dry-run`).
- A one-off change to a single, textually-unique line you already located → a manual **Edit** is fine.

## Gotchas

- Hammer++ writes **LF** line endings, even when the surrounding repo is CRLF. The tool detects & preserves line endings per file — don't normalize them.
- Keep any script output you add **ASCII**: Windows consoles with a legacy code page (e.g. cp1251, cp1252) crash on non-ASCII prints.
- `id`s are shared across solids/sides/entities and must be unique; always allocate with `new-id` / the tool's allocator, never hand-pick.
- Keys repeat (`vertices_plus` `"v"`, multiple same-named I/O outputs). Never model VMF as a plain dict.
- Hammer++ `*_plus` blocks must be preserved — the tool does this automatically.
- `move` does not apply texture lock; textures shift relative to moved geometry (acceptable for most edits, re-align if it matters).

## After editing

Re-run `python scripts/vmf.py lint MAP.vmf` and, for sanity, `roundtrip` on the
result. If the map is under version control, diff it to confirm only the intended
lines changed.
