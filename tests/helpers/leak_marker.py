"""Deterministic process-liveness marker for containment tests.

A fixture process creates a named mutex and holds the handle for its lifetime.
The Windows kernel closes all handles when the process terminates and destroys
the named object with the last handle, so the object's existence is a direct
survivor signal that does not depend on PID publication, PID reuse or process
enumeration rights:

    OpenMutexW(name, SYNCHRONIZE, FALSE) succeeds  -> some process still holds it
    OpenMutexW fails with ERROR_FILE_NOT_FOUND     -> no holder in this session

The negative conclusion is bounded by one documented window: a process killed
before it reaches its first marker call never holds the marker. Every fixture
process therefore creates the marker as one of its first actions, which keeps
that window smaller than the PID-file publication window it replaces. A probe
handle must be closed immediately; caching it would keep the name alive past
process death and manufacture a false survivor.

CLI (used by negative controls and tree fixtures):

    python leak_marker.py --hold <name> <seconds> [ready_file]
    python leak_marker.py --tree-hold <name> <seconds> <ready_file>
"""

from __future__ import annotations

import argparse
import ctypes
import pathlib
import subprocess
import sys
import time
import uuid
from ctypes import wintypes

SYNCHRONIZE = 0x00100000
ERROR_FILE_NOT_FOUND = 2
ERROR_ALREADY_EXISTS = 183
MARKER_PROBE_TIMEOUT_SECONDS = 3.0
MARKER_PROBE_INTERVAL_SECONDS = 0.05

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = wintypes.HANDLE
KERNEL32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.OpenMutexW.restype = wintypes.HANDLE
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL


def new_marker_name() -> str:
    """A per-run session-unique marker name; no cross-run collisions."""
    return "Local\\CoreGuard-" + uuid.uuid4().hex


def create_marker(name: str) -> int:
    """Fixture side: create or open the marker and hold it for process lifetime."""
    ctypes.set_last_error(0)
    handle = KERNEL32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(
            "CreateMutexW failed for %s: %d" % (name, ctypes.get_last_error())
        )
    return int(handle)


def hold_marker(name: str) -> None:
    """Fixture side: hold the marker handle until the process exits.

    The raw handle is intentionally not closed anywhere: the kernel releases
    it at process termination, which is exactly the lifetime the oracle reads.
    """
    create_marker(name)


def marker_holder_exists(name: str) -> bool:
    """Oracle side: True when some process still holds the marker.

    ERROR_FILE_NOT_FOUND is the only error treated as "no holder"; any other
    failure raises, so an unreadable state can never be reported as clean.
    """
    ctypes.set_last_error(0)
    handle = KERNEL32.OpenMutexW(SYNCHRONIZE, False, name)
    if handle:
        KERNEL32.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == ERROR_FILE_NOT_FOUND:
        return False
    raise OSError("OpenMutexW failed for %s: %d" % (name, error))


def wait_marker_absent(
    name: str,
    timeout_seconds: float = MARKER_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Poll for teardown; True only when no holder remains within the window."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not marker_holder_exists(name):
            return True
        time.sleep(MARKER_PROBE_INTERVAL_SECONDS)
    return not marker_holder_exists(name)


def _hold(name: str, seconds: float, ready_file: pathlib.Path | None) -> int:
    create_marker(name)
    if ready_file is not None:
        ready_file.write_text("marker-ready", encoding="ascii")
    time.sleep(seconds)
    return 0


def _tree_hold(name: str, seconds: float, ready_file: pathlib.Path) -> int:
    create_marker(name)
    child = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--hold-ready",
            name,
            str(seconds),
            str(ready_file),
        ],
        close_fds=True,
    )
    try:
        time.sleep(seconds)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", nargs=2, metavar=("NAME", "SECONDS"))
    parser.add_argument("--hold-ready", nargs=3, metavar=("NAME", "SECONDS", "READY"))
    parser.add_argument("--tree-hold", nargs=3, metavar=("NAME", "SECONDS", "READY"))
    args = parser.parse_args()
    if args.hold is not None:
        return _hold(args.hold[0], float(args.hold[1]), None)
    if args.hold_ready is not None:
        return _hold(
            args.hold_ready[0],
            float(args.hold_ready[1]),
            pathlib.Path(args.hold_ready[2]),
        )
    if args.tree_hold is not None:
        return _tree_hold(
            args.tree_hold[0],
            float(args.tree_hold[1]),
            pathlib.Path(args.tree_hold[2]),
        )
    parser.error("no mode selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
