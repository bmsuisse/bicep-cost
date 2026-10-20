"""Reject oversized source files before commit."""

import sys
from pathlib import Path


def main(paths: list[str]) -> int:
    failed = False
    for name in paths:
        path = Path(name)
        if not path.is_file():
            continue
        limit = 1800 if path.name.startswith("test_") else 1200
        if path.suffix == ".md":
            limit = 1000
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError:
            continue
        if count > limit:
            print(f"{path}: {count} lines exceeds {limit}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
