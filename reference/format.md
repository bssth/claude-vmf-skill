# VMF format reference

Read this when you need format details beyond the cheat-sheet in `SKILL.md`.
Everything here is verified against real Hammer++ output (several multi-MB maps).

## Lexical syntax

- A file is a sequence of **blocks**. A block is `Name` then `{ ... }`, with the
  name and each brace on their **own line**.
- Inside a block, lines are either a **keyvalue** `"key" "value"` or a nested
  block. Keyvalues and nested blocks may **interleave** (a `side` lists `id`,
  `plane`, then a `vertices_plus` block, then `material`, `uaxis`, ...).
- Indentation is **one tab per nesting level**. Strings are double-quoted; block
  names are bare words. Files end with a trailing newline.
- **Keys can repeat** within a block. This is load-bearing: `vertices_plus` has
  several `"v"`; `connections` has several outputs that may share a name. A dict
  collapses them — always keep an ordered list (the tool does).
- Line endings: Hammer++ writes **LF**. Comments (`// ...`) are legal but Hammer never writes them.

## Top-level blocks (in typical order)

| Block | Purpose |
|---|---|
| `versioninfo` | editor/format/map version, `prefab` flag |
| `visgroups` | visibility groups (tree of `visgroup`) |
| `viewsettings` | grid/snap editor state |
| `palette_plus`, `colorcorrection_plus`, `light_plus`, `postprocess_plus`, `bgimages_plus` | **Hammer++ only** — preserve verbatim, vanilla Hammer ignores them |
| `world` | the map geometry root: worldspawn keyvalues + many `solid` blocks |
| `entity` | one per entity (repeated); point or brush |
| `hidden` | wraps `solid`/`entity` hidden in the editor (same inner structure) |
| `cameras`, `cordon`/`cordons` | editor viewport cameras and cordon bounds |

## world / solid / side

```
world
{
    "id" "1"
    "classname" "worldspawn"
    "skyname" "sky_day01_01"
    solid
    {
        "id" "16"
        side
        {
            "id" "42"
            "plane" "(-320 -576 -320) (-512 -576 -320) (-512 -576 -192)"
            vertices_plus            // Hammer++ cached face winding (vbsp ignores it)
            {
                "v" "-320 -576 -320"
                ...
            }
            "material" "DEV/DEV_MEASUREGENERIC01"
            "uaxis" "[1 0 0 0] 0.25"   // [Ux Uy Uz offset] scale  (texture U axis)
            "vaxis" "[0 0 -1 0] 0.25"
            "rotation" "0"
            "lightmapscale" "16"
            "smoothing_groups" "0"
        }
        editor { "color" "0 180 137" ... }
    }
}
```

- A **solid** is a convex brush defined by the intersection of its sides' half-spaces.
- A **side**'s `plane` is 3 points `(x y z) (x y z) (x y z)`. The plane normal is
  `(p1-p0) × (p2-p0)` and **must point out of the solid**. vbsp uses only the plane;
  `vertices_plus` is a Hammer++ display cache. The `box` command orients normals
  outward automatically.
- `uaxis`/`vaxis` map the texture: `[x y z offset] scale`. Must not be parallel to
  the face normal. Common scale is `0.25`.
- A `dispinfo` block inside a side turns it into a **displacement** (terrain):
  `power`, `startposition`, and `normals`/`distances`/`offsets`/`alphas`/
  `triangle_tags`/`allowed_verts` rows (`"row0" ...`). Displacement sides must be
  4-sided. Leave dispinfo data alone unless you understand it.

## entity (point vs brush)

```
entity
{
    "id" "14893"
    "classname" "logic_relay"
    "targetname" "my_relay"
    "origin" "-1553 -902 -315"      // point entities position via origin
    connections { ... }              // I/O, optional
    // brush entities instead carry one or more `solid { ... }` blocks (no origin)
    editor { "color" "220 30 220" "visgroupshown" "1" "visgroupautoshown" "1" }
}
```

- **Point entity**: positioned by `origin`. **Brush entity**: contains `solid`
  blocks, no `origin` (e.g. `func_detail`, `trigger_multiple`, `func_brush`).
- Arbitrary keyvalues depend on the entity's FGD class (`health`, `spawnflags`,
  `StartDisabled`, model paths, etc.).

## connections (I/O)

```
connections
{
    "OnTrigger" "target,Input,parameter,delay,times_to_fire"
    "OnTrigger" "other,Color,0 160 0,0,-1"     // same output may appear many times
    "OnBreak"   "door1,Open,,0.5,1"
}
```

- Value fields are comma-separated: **target, input, parameter, delay,
  times_to_fire** (`-1` = unlimited; `0` delay = immediate). Some legacy VMFs use
  the `\x1b` (ESC) separator instead of comma; the tool handles both.
- `target` may be a literal targetname, a wildcard (`door_*`), or a special token
  (`!self`, `!player`, `!activator`, `!caller`, ...). The `lint` command flags
  targets that match no targetname and aren't special — possibly broken outputs
  (or runtime-spawned via `point_template`/instances).

## Coordinates & units

- Right-handed, **Z is up**. 1 unit ≈ 1 inch; player is ~72 units tall; standard
  grid steps are powers of two (...16, 32, 64...).
- `origin`/coords can be floats. Keep formatting tidy: integers without trailing
  `.0` (the tool's coordinate formatter does this).

## id numbering

- Every `solid`, `side`, and `entity` has an `id`, drawn from **one shared space**
  and meant to be unique map-wide. Copy-paste / prefab / vmf-merge can introduce
  duplicates; Hammer silently reassigns them on load, so `lint` reports dup ids as
  INFO, not an error. When adding objects, allocate `max(id)+1` (the `new-id`
  command / `IdAllocator`) so you never collide.

## Pitfalls checklist

- Don't read whole maps into context — query/slice with the tool.
- Don't drop `*_plus` blocks or rewrite formatting — breaks the user's Hammer++ file.
- Don't model repeated keys as a dict.
- Don't hand-pick ids.
- Don't hand-author brush planes unless you orient normals outward and keep the
  brush convex — use `box`/the generator.
- After edits, `lint` + a git diff; the byte-exact serializer means the diff shows
  exactly (and only) your change.
