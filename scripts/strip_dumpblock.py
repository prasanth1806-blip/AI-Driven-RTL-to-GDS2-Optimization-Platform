#!/usr/bin/env python3
"""Strip the simulation-only $dumpfile/$dumpvars initial block from a
SystemVerilog file before it's handed to Yosys for synthesis.

Usage: strip_dumpblock.py <input.sv> <output.sv>
"""
import re
import sys


def main():
    if len(sys.argv) != 3:
        print("Usage: strip_dumpblock.py <input.sv> <output.sv>", file=sys.stderr)
        sys.exit(2)

    src_path, out_path = sys.argv[1], sys.argv[2]

    pattern = re.compile(
        r"initial\s*begin\s*\$dumpfile\([^)]*\)\s*;\s*\$dumpvars\([^)]*\)\s*;\s*end",
        re.IGNORECASE | re.DOTALL,
    )

    with open(src_path, "r") as f:
        src = f.read()

    stripped = pattern.sub("", src)

    with open(out_path, "w") as f:
        f.write(stripped)


if __name__ == "__main__":
    main()