#!/usr/bin/env python3
"""Byte-exact round-trip test: parse(src) -> serialize must equal src for every
VMF found. This is the safety net that lets the editing commands trust the
parser. Run it over your maps before relying on any vmf.py mutation.

Usage:
    python test_roundtrip.py [GLOB ...]

GLOBs support ** (e.g. "maps/**/*.vmf"). With no args it scans **/*.vmf under
the current working directory.
"""

import glob
import os
import sys
import difflib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf  # noqa: E402


def _first_diff(a: str, b: str) -> str:
    sm = difflib.SequenceMatcher(None, a, b)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            ctx_a = repr(a[max(0, i1 - 30):i2 + 30])
            ctx_b = repr(b[max(0, j1 - 30):j2 + 30])
            line = a.count("\n", 0, i1) + 1
            return f"    first diff near src line {line}:\n      src: {ctx_a}\n      out: {ctx_b}"
    return ""


def main(argv):
    if argv:
        files = []
        for g in argv:
            files.extend(glob.glob(g, recursive=True))
    else:
        files = glob.glob(os.path.join(os.getcwd(), "**", "*.vmf"), recursive=True)
    files = sorted(set(files))
    if not files:
        print("no .vmf files found", file=sys.stderr)
        return 2

    ok = 0
    fail = 0
    for path in files:
        with open(path, "r", encoding="utf-8", newline="") as f:
            src = f.read()
        try:
            doc = vmf.parse(src)
            out = vmf.serialize(doc)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {path}: {type(e).__name__}: {e}")
            fail += 1
            continue
        if out == src:
            print(f"OK   {os.path.basename(path):<24} {len(src):>9} bytes")
            ok += 1
        else:
            print(f"DIFF {os.path.basename(path):<24} src {len(src)} -> out {len(out)}")
            print(_first_diff(src, out))
            fail += 1

    print(f"\n{ok} ok, {fail} failed, {len(files)} total")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
