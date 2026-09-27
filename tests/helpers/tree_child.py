"""Small deterministic process-tree fixture for the Windows acceptance test."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import leak_marker  # noqa: E402


def main() -> int:
    arguments = sys.argv[1:]
    marker: str | None = None
    if len(arguments) >= 2 and arguments[0] == "--marker":
        marker = arguments[1]
        arguments = arguments[2:]

    if len(arguments) == 2 and arguments[0] == "--grandchild":
        if marker is not None:
            leak_marker.hold_marker(marker)
        pathlib.Path(arguments[1]).write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(1)

    if len(arguments) != 1:
        return 2
    pid_file = pathlib.Path(arguments[0])
    if marker is not None:
        leak_marker.hold_marker(marker)
    grandchild_argv = [sys.executable, __file__]
    if marker is not None:
        grandchild_argv.extend(["--marker", marker])
    grandchild_argv.extend(["--grandchild", str(pid_file) + ".grandchild"])
    grandchild = subprocess.Popen(grandchild_argv, close_fds=True)
    pid_file.write_text(
        "%d %d" % (os.getpid(), grandchild.pid), encoding="ascii"
    )
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
