"""Bounded CPU-time fixtures for job-wide process-tree enforcement tests."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time


def burn(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    value = 0
    while time.monotonic() < deadline:
        value = (value + 1) & 0xFFFFFFFF
    if value == -1:
        raise AssertionError("unreachable")


def write_pids(path: pathlib.Path, *pids: int) -> None:
    path.write_text(" ".join(str(pid) for pid in pids), encoding="ascii")


def spawn_child(pid_file: pathlib.Path, seconds: float) -> int:
    child = subprocess.Popen(
        [sys.executable, __file__, "--burn", str(seconds)], close_fds=True
    )
    write_pids(pid_file, os.getpid(), child.pid)
    time.sleep(seconds + 5.0)
    return 0


def spawn_grandchild(pid_file: pathlib.Path, seconds: float) -> int:
    child_info = pid_file.with_suffix(".childinfo")
    child = subprocess.Popen(
        [sys.executable, __file__, "--grandchild", str(child_info), str(seconds)],
        close_fds=True,
    )
    deadline = time.monotonic() + 3.0
    while not child_info.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not child_info.is_file():
        return 10
    child_pid, grandchild_pid = [
        int(value) for value in child_info.read_text(encoding="ascii").split()
    ]
    write_pids(pid_file, os.getpid(), child_pid, grandchild_pid)
    time.sleep(seconds + 5.0)
    return 0


def spawn_grandchild_worker(info_file: pathlib.Path, seconds: float) -> int:
    grandchild = subprocess.Popen(
        [sys.executable, __file__, "--burn", str(seconds)], close_fds=True
    )
    write_pids(info_file, os.getpid(), grandchild.pid)
    time.sleep(seconds + 5.0)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--burn", type=float)
    parser.add_argument("--spawn-child", nargs=2, metavar=("PID_FILE", "SECONDS"))
    parser.add_argument("--tree", nargs=2, metavar=("PID_FILE", "SECONDS"))
    parser.add_argument("--grandchild", nargs=2, metavar=("INFO_FILE", "SECONDS"))
    args = parser.parse_args()
    if args.burn is not None:
        burn(args.burn)
        return 0
    if args.spawn_child is not None:
        return spawn_child(pathlib.Path(args.spawn_child[0]), float(args.spawn_child[1]))
    if args.tree is not None:
        return spawn_grandchild(pathlib.Path(args.tree[0]), float(args.tree[1]))
    if args.grandchild is not None:
        return spawn_grandchild_worker(
            pathlib.Path(args.grandchild[0]), float(args.grandchild[1])
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
