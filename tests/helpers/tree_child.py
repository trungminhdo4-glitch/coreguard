"""Small deterministic process-tree fixture for the Windows acceptance test."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--grandchild":
        pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(1)

    if len(sys.argv) != 2:
        return 2
    pid_file = pathlib.Path(sys.argv[1])
    grandchild = subprocess.Popen(
        [sys.executable, __file__, "--grandchild", str(pid_file) + ".grandchild"],
        close_fds=True,
    )
    pid_file.write_text(
        "%d %d" % (os.getpid(), grandchild.pid), encoding="ascii"
    )
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
