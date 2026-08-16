"""Lossless argv oracle for Windows CreateProcessW round-trip tests."""

from __future__ import annotations

import json
import sys


def main() -> int:
    print(json.dumps(sys.argv[1:], ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
