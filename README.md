# claude-vmf-skill

A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill for working with
Source Engine **VMF** map files (Hammer / Hammer++): inspect, query, validate,
surgically edit and generate geometry — without ever loading a multi-megabyte map
into the model's context.

Real maps are often 5-10 MB and hundreds of thousands of lines. Reading one raw
burns the context window instantly. This skill gives the agent a CLI that returns
summaries and small, exact slices instead, plus edit commands that change only
what was asked for.

## What's inside

```
SKILL.md              skill entry point: triggers, cheat-sheet, decision tree, gotchas
reference/format.md   VMF format reference (loaded on demand)
scripts/vmf.py        parser / model / serializer + CLI (pure stdlib, Python 3.8+)
scripts/test_roundtrip.py   byte-exact parse->serialize check over a tree of maps
```

`vmf.py` commands:

| Kind | Commands |
|---|---|
| Read | `stats`, `find`, `get`, `io-graph`, `refs` |
| Validate | `lint`, `roundtrip` |
| Edit (all support `--dry-run`) | `set-kv`, `add-io`, `rm-io`, `replace-material`, `move` |
| Generate | `new-id`, `box`, `add-entity` |

Key properties:

- **Byte-exact round-trip.** Parsing and re-serializing an unchanged map reproduces
  it byte for byte (line endings included), so a diff after an edit shows only the
  edit.
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

## License

MIT, see [LICENSE](LICENSE).
