"""Leak-evidence tests for the named-object survivor oracle.

The PID-file oracles in this suite can only check processes that wrote a PID
before they were killed. The fixtures here never publish a PID: a fixture
process holds a session-unique named mutex for its lifetime, so process death
is observable through the kernel object alone. Presence proves a survivor;
absence is a bounded clearance signal for cooperative fixture processes whose
first action is the marker call. None of these tests claim that no process can
ever escape a job - they observe the documented marker window only.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEAK_HELPER = ROOT / "tests" / "helpers" / "leak_marker.py"
DEFAULT_EXE = ROOT / "build" / "coreguard.exe"
FALLBACK_EXE = ROOT / "build" / "Release" / "coreguard.exe"
sys.path.insert(0, str(ROOT / "tests"))

from helpers import leak_marker  # noqa: E402
import verification  # noqa: E402

MARKER_WAIT_SECONDS = 15.0
TREE_CHILD_HOLD_SECONDS = 15


def coreguard_exe() -> pathlib.Path:
    if DEFAULT_EXE.is_file():
        return DEFAULT_EXE
    return FALLBACK_EXE


class LeakEvidenceTests(unittest.TestCase):
    def test_oracle_detects_survivor_without_any_pid_publication(self) -> None:
        marker = leak_marker.new_marker_name()
        with tempfile.TemporaryDirectory(prefix="coreguard-marker-") as temp:
            readiness = pathlib.Path(temp) / "ready.txt"
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(LEAK_HELPER),
                    "--hold-ready",
                    marker,
                    "30",
                    str(readiness),
                ],
                close_fds=True,
            )
            try:
                deadline = time.monotonic() + MARKER_WAIT_SECONDS
                while not leak_marker.marker_holder_exists(marker):
                    if child.poll() is not None:
                        self.fail("fixture exited before creating the marker")
                    if time.monotonic() > deadline:
                        self.fail("fixture never created the marker")
                    time.sleep(0.02)
                self.assertTrue(
                    readiness.is_file(), "survivor readiness was not observed"
                )
            finally:
                child.kill()
                child.wait(timeout=MARKER_WAIT_SECONDS)
        self.assertTrue(
            leak_marker.wait_marker_absent(marker),
            "marker survived the fixture termination",
        )

    def test_marked_tree_without_pid_publication_leaves_no_holder(self) -> None:
        exe = coreguard_exe()
        if not exe.is_file():
            self.skipTest("coreguard executable not found")
        marker = leak_marker.new_marker_name()
        with tempfile.TemporaryDirectory(prefix="coreguard-marked-tree-") as temp:
            readiness = pathlib.Path(temp) / "child-ready.txt"
            command = [
                sys.executable,
                str(LEAK_HELPER),
                "--tree-hold",
                marker,
                str(TREE_CHILD_HOLD_SECONDS),
                str(readiness),
            ]

            def run(timeout_ms: int):
                return verification.run_coreguard(
                    exe, timeout_ms, command, timeout_seconds=30.0
                )

            payload, completed = run(800)
            if not readiness.is_file():
                payload, completed = run(2000)
            self.assertTrue(readiness.is_file(), "marked tree child never became ready")
            self.assertEqual(completed.returncode, 124)
            self.assertEqual(payload["status"], "timeout")
            self.assertTrue(payload["cleanup_ok"])
            self.assertFalse((pathlib.Path(temp) / "pids.txt").exists())
            self.assertTrue(
                leak_marker.wait_marker_absent(marker),
                "contained tree left a survivor holder",
            )

    def test_marked_fixture_natural_exit_leaves_no_holder(self) -> None:
        exe = coreguard_exe()
        if not exe.is_file():
            self.skipTest("coreguard executable not found")
        marker = leak_marker.new_marker_name()
        with tempfile.TemporaryDirectory(prefix="coreguard-marked-exit-") as temp:
            readiness = pathlib.Path(temp) / "ready.txt"
            payload, completed = verification.run_coreguard(
                exe,
                5000,
                [
                    sys.executable,
                    str(LEAK_HELPER),
                    "--hold-ready",
                    marker,
                    "1",
                    str(readiness),
                ],
                timeout_seconds=30.0,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(payload["status"], "exited")
            self.assertTrue(payload["cleanup_ok"])
        self.assertTrue(
            leak_marker.wait_marker_absent(marker),
            "natural exit left a survivor holder",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
