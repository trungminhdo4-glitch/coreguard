"""Controlled process-tree workloads for native Job Object accounting tests."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time


PAGE_SIZE = 4096
SCRIPT = pathlib.Path(__file__).resolve()


def burn(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    value = 0
    while time.monotonic() < deadline:
        value = (value + 1) & 0xFFFFFFFF
    if value == -1:
        raise AssertionError("unreachable")


def allocate(megabytes: int) -> bytearray:
    data = bytearray(megabytes * 1024 * 1024)
    for index in range(0, len(data), PAGE_SIZE):
        data[index] = 1
    return data


def do_io(path: pathlib.Path, size: int = 2 * 1024 * 1024) -> None:
    path.write_bytes(b"x" * size)
    if len(path.read_bytes()) != size:
        raise AssertionError("controlled I/O roundtrip failed")


def write_report(path: pathlib.Path, **values: object) -> None:
    path.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")


def run_cpu_child(report: pathlib.Path, count: int, seconds: float, parallel: bool) -> int:
    children = []
    for _ in range(count):
        children.append(
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--burn", str(seconds)],
                close_fds=True,
            )
        )
        if not parallel:
            children[-1].wait(timeout=10)
    if parallel:
        for child in children:
            child.wait(timeout=10)
    write_report(report, child_count=count, parallel=parallel)
    return 0


def run_grandchild(report: pathlib.Path, seconds: float) -> int:
    child = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--grandchild-worker", str(seconds)],
        close_fds=True,
    )
    child.wait(timeout=10)
    write_report(report, child_count=1, grandchild=True)
    return 0


def run_grandchild_worker(seconds: float) -> int:
    grandchild = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--burn", str(seconds)],
        close_fds=True,
    )
    grandchild.wait(timeout=10)
    return 0


def run_io(report: pathlib.Path, root_path: pathlib.Path, child_path: pathlib.Path) -> int:
    child = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--io-child", str(child_path)],
        close_fds=True,
    )
    do_io(root_path)
    child.wait(timeout=10)
    write_report(report, root_bytes=root_path.stat().st_size, child_bytes=child_path.stat().st_size)
    return 0


def run_memory(report: pathlib.Path, root_megabytes: int, child_megabytes: int, seconds: float) -> int:
    root_data = allocate(root_megabytes)
    child = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--memory-child", str(child_megabytes), str(seconds)],
        close_fds=True,
    )
    child.wait(timeout=10)
    write_report(report, root_bytes=len(root_data), child_bytes=child_megabytes * 1024 * 1024)
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--burn":
        burn(float(sys.argv[2]))
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "--orphan-child":
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--sleep", sys.argv[3]],
            close_fds=True,
        )
        write_report(pathlib.Path(sys.argv[2]), child_pid=child.pid)
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--sleep":
        time.sleep(float(sys.argv[2]))
        return 0
    if len(sys.argv) == 4 and sys.argv[1] in ("--sequential", "--parallel"):
        mode = sys.argv[1]
        return run_cpu_child(
            pathlib.Path(sys.argv[2]), int(sys.argv[3]), 0.12, mode == "--parallel"
        )
    if len(sys.argv) == 3 and sys.argv[1] == "--grandchild":
        return run_grandchild(pathlib.Path(sys.argv[2]), 0.12)
    if len(sys.argv) == 3 and sys.argv[1] == "--grandchild-worker":
        return run_grandchild_worker(float(sys.argv[2]))
    if len(sys.argv) == 5 and sys.argv[1] == "--io":
        return run_io(
            pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
        )
    if len(sys.argv) == 6 and sys.argv[1] == "--memory":
        return run_memory(
            pathlib.Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
        )
    if len(sys.argv) == 3 and sys.argv[1] == "--io-child":
        do_io(pathlib.Path(sys.argv[2]))
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "--memory-child":
        data = allocate(int(sys.argv[2]))
        time.sleep(float(sys.argv[3]))
        if not data:
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
