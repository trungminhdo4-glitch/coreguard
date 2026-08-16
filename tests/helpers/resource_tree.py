"""Bounded process-tree fixtures for job-wide memory-limit tests."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time


PAGE_SIZE = 4096


def touch_memory(megabytes: int) -> bytearray:
    data = bytearray(megabytes * 1024 * 1024)
    for index in range(0, len(data), PAGE_SIZE):
        data[index] = 1
    return data


def sleep_forever() -> None:
    while True:
        time.sleep(1)


def allocate_process(megabytes: int, pid_file: pathlib.Path, catch: bool) -> int:
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    try:
        data = touch_memory(megabytes)
    except MemoryError:
        if catch:
            print("allocation failed", flush=True)
            return 7
        raise
    if not data:
        return 8
    sleep_forever()
    return 0


def spawn_child(pid_file: pathlib.Path, megabytes: int, catch: bool) -> int:
    child_pid_file = pid_file.with_suffix(".child")
    child_args = [
        sys.executable,
        __file__,
        "--allocate",
        str(megabytes),
        str(child_pid_file),
    ]
    if catch:
        child_args.append("--catch")
    child = subprocess.Popen(child_args, close_fds=True)
    pid_file.write_text(f"{os.getpid()} {child.pid}", encoding="ascii")
    sleep_forever()
    return 0


def spawn_grandchild(info_file: pathlib.Path, megabytes: int) -> int:
    grandchild_pid_file = info_file.with_suffix(".grandchild")
    grandchild = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--allocate",
            str(megabytes),
            str(grandchild_pid_file),
        ],
        close_fds=True,
    )
    info_file.write_text(
        f"{os.getpid()} {grandchild.pid}", encoding="ascii"
    )
    sleep_forever()
    return 0


def spawn_tree(pid_file: pathlib.Path, megabytes: int) -> int:
    child_info = pid_file.with_suffix(".childinfo")
    child = subprocess.Popen(
        [sys.executable, __file__, "--grandchild", str(child_info), str(megabytes)],
        close_fds=True,
    )
    deadline = time.monotonic() + 3
    while not child_info.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not child_info.is_file():
        return 10
    child_pid, grandchild_pid = [
        int(value) for value in child_info.read_text(encoding="ascii").split()
    ]
    pid_file.write_text(
        f"{os.getpid()} {child_pid} {grandchild_pid}", encoding="ascii"
    )
    sleep_forever()
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--allocate":
        return allocate_process(
            int(sys.argv[2]), pathlib.Path(sys.argv[3]), "--catch" in sys.argv[4:]
        )
    if len(sys.argv) == 4 and sys.argv[1] == "--spawn-child":
        return spawn_child(pathlib.Path(sys.argv[2]), int(sys.argv[3]), False)
    if len(sys.argv) == 4 and sys.argv[1] == "--spawn-child-catch":
        return spawn_child(pathlib.Path(sys.argv[2]), int(sys.argv[3]), True)
    if len(sys.argv) == 4 and sys.argv[1] == "--grandchild":
        return spawn_grandchild(pathlib.Path(sys.argv[2]), int(sys.argv[3]))
    if len(sys.argv) == 4 and sys.argv[1] == "--tree":
        return spawn_tree(pathlib.Path(sys.argv[2]), int(sys.argv[3]))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
