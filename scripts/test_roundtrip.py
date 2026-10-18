#!/usr/bin/env python3
"""Byte-exact round-trip test: parse(src) -> serialize must equal src for every
VMF found (UTF-8 or, for old cp1252 sources, latin-1; compared as bytes, the way
vmf.load()/save() handle them). This is the safety net that lets the editing commands trust the
parser. Run it over your maps before relying on any vmf.py mutation.

Usage:
    python test_roundtrip.py [GLOB ...]

GLOBs support ** (e.g. "maps/**/*.vmf"). With no args it scans **/*.vmf under
the current working directory.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmf  # noqa: E402


def _first_diff(a: bytes, b: bytes) -> str:
    i = len(os.path.commonprefix([a, b]))
    ctx_a = ascii(a[max(0, i - 30):i + 30])
    ctx_b = ascii(b[max(0, i - 30):i + 30])
    line = a.count(b"\n", 0, i) + 1
    return f"    first diff near src line {line}:\n      src: {ctx_a}\n      out: {ctx_b}"


def main(argv):
    vmf.safe_stdio()
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
    non_utf8 = 0
    for path in files:
        with open(path, "rb") as f:
            src = f.read()
        try:
            doc = vmf.parse_bytes(src)
            out = vmf.serialize_bytes(doc)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {path}: {type(e).__name__}: {e}")
            fail += 1
            continue
        note = ""
        if doc.encoding != "utf-8":
            note = f"  [{doc.encoding}]"
            non_utf8 += 1
        if out == src:
            print(f"OK   {os.path.basename(path):<24} {len(src):>9} bytes{note}")
            ok += 1
        else:
            print(f"DIFF {os.path.basename(path):<24} src {len(src)} -> out {len(out)}{note}")
            print(_first_diff(src, out))
            fail += 1

    print(f"\n{ok} ok, {fail} failed, {len(files)} total"
          + (f" ({non_utf8} not UTF-8, round-tripped as latin-1)" if non_utf8 else ""))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
