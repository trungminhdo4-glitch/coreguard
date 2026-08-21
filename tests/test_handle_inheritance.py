"""Regression tests for explicit Windows child-handle inheritance."""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import unittest
from ctypes import wintypes


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "helpers" / "handle_probe.py"
BUILD = ROOT / "build"
BASELINE_BUILD = ROOT.parent / "build"
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class HandleInheritanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows-only handle inheritance regression")
        configured = os.environ.get("COREGUARD_EXE")
        candidates = (BUILD / "coreguard.exe", BUILD / "Release" / "coreguard.exe")
        cls.exe = pathlib.Path(configured).resolve() if configured else next(
            (candidate.resolve() for candidate in candidates if candidate.is_file()),
            candidates[0].resolve(),
        )
        if not cls.exe.is_file():
            raise unittest.SkipTest("build coreguard.exe first")
        configured_baseline = os.environ.get("COREGUARD_OLD_EXE")
        baseline_candidates = (
            BASELINE_BUILD / "Release" / "coreguard.exe",
            BASELINE_BUILD / "coreguard.exe",
        )
        cls.baseline_exe = (
            pathlib.Path(configured_baseline).resolve()
            if configured_baseline
            else next(
                (
                    candidate.resolve()
                    for candidate in baseline_candidates
                    if candidate.is_file()
                ),
                None,
            )
        )

    @staticmethod
    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _run_probe(
        self,
        executable: pathlib.Path,
        handles: list[int],
    ) -> tuple[dict, dict]:
        completed = subprocess.run(
            [
                str(executable),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--",
                sys.executable,
                str(HELPER),
                *(str(handle) for handle in handles),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            close_fds=False,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "", completed.stderr)
        outer = json.loads(completed.stdout)
        self.assertEqual(outer["status"], "exited", outer)
        probe = json.loads(outer["stdout"])
        return outer, probe

    def _run_identity_probe(
        self,
        executable: pathlib.Path,
    ) -> tuple[dict, dict, int]:
        kernel32 = self._kernel32()
        sentinel = kernel32.CreateEventW(None, False, False, None)
        self.assertTrue(sentinel)
        try:
            self.assertTrue(
                kernel32.SetHandleInformation(
                    sentinel, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
                ),
                ctypes.get_last_error(),
            )
            outer, probe = self._run_probe(executable, [int(sentinel)])
            wait_result = kernel32.WaitForSingleObject(sentinel, 0)
            return outer, probe, wait_result
        finally:
            kernel32.CloseHandle(sentinel)

    def test_unrelated_event_is_leaked_by_old_binary_and_blocked_by_candidate(self) -> None:
        if self.baseline_exe is None:
            self.skipTest("set COREGUARD_OLD_EXE or provide the published v0.1.0 binary")
        self.assertNotEqual(self.baseline_exe, self.exe)

        _, old_probe, old_wait = self._run_identity_probe(self.baseline_exe)
        self.assertTrue(old_probe["valid"], old_probe)
        self.assertTrue(old_probe["set_event_success"], old_probe)
        self.assertEqual(old_wait, WAIT_OBJECT_0)

        _, new_probe, new_wait = self._run_identity_probe(self.exe)
        # Numeric handle values can alias another child object; the original
        # parent event's wait is the object-identity oracle.
        self.assertIn("set_event_success", new_probe)
        self.assertEqual(new_wait, WAIT_TIMEOUT)

    def test_stdin_pipe_is_preserved_through_allowlist(self) -> None:
        payload = "coreguard-stdin-allowlist\n"
        code = (
            "import sys; "
            "data = sys.stdin.read(%d); "
            "sys.stdout.write(data)"
        ) % len(payload)
        completed = subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--",
                sys.executable,
                "-c",
                code,
            ],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            close_fds=False,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "", completed.stderr)
        outer = json.loads(completed.stdout)
        self.assertEqual(outer["status"], "exited", outer)
        self.assertEqual(outer["stdout"], payload)

    def test_parallel_children_do_not_receive_each_others_sentinels(self) -> None:
        kernel32 = self._kernel32()
        sentinels: list[wintypes.HANDLE] = []
        processes: list[subprocess.Popen[str]] = []
        try:
            for _ in range(8):
                sentinel = kernel32.CreateEventW(None, False, False, None)
                self.assertTrue(sentinel)
                self.assertTrue(
                    kernel32.SetHandleInformation(
                        sentinel, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
                    ),
                    ctypes.get_last_error(),
                )
                sentinels.append(sentinel)

            handles = [str(int(sentinel)) for sentinel in sentinels]
            for _ in range(8):
                processes.append(
                    subprocess.Popen(
                        [
                            str(self.exe),
                            "run",
                            "--json",
                            "--timeout-ms",
                            "5000",
                            "--",
                            sys.executable,
                            str(HELPER),
                            *handles,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="strict",
                        close_fds=False,
                    )
                )

            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(stderr, "", stderr)
                outer = json.loads(stdout)
                self.assertEqual(outer["status"], "exited", outer)
                probes = json.loads(outer["stdout"])["handles"]
                self.assertEqual(len(probes), len(sentinels))
                for probe in probes:
                    self.assertIn("set_event_success", probe)

            for sentinel in sentinels:
                self.assertEqual(
                    kernel32.WaitForSingleObject(sentinel, 0),
                    WAIT_TIMEOUT,
                )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            for sentinel in sentinels:
                kernel32.CloseHandle(sentinel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
