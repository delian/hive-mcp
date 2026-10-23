"""Fail if any Python file under src/ exceeds the size limit (default 16 KB, warn at 12 KB)."""

from __future__ import annotations

import sys
from pathlib import Path

HARD = 16 * 1024
WARN = 12 * 1024
TARGET = 8 * 1024


def main(roots: list[str]) -> int:
    failed = False
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            size = path.stat().st_size
            if size > HARD:
                print(f"FAIL {path}: {size} bytes > {HARD}")
                failed = True
            elif size > WARN:
                print(f"WARN {path}: {size} bytes > {WARN} (target {TARGET})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["src"]))
