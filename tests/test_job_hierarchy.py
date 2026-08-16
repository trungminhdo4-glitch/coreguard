"""Focused containment and compatibility experiments."""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
COREGUARD_CANDIDATES = (BUILD / "coreguard.exe", BUILD / "Release" / "coreguard.exe")
COREGUARD = next(
    (candidate.resolve() for candidate in COREGUARD_CANDIDATES if candidate.is_file()),
    COREGUARD_CANDIDATES[0].resolve(),
)
PROBE = ROOT / "tests" / "helpers" / "job_hierarchy_probe.py"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def process_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class JobHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Job hierarchy probes require Windows Job Objects")
        if not COREGUARD.is_file():
            raise unittest.SkipTest(f"missing executable: {COREGUARD}")

    def run_coreguard(self, target_args: list[str], coreguard_args: list[str] | None = None) -> tuple[dict, dict, str]:
        coreguard_args = coreguard_args or []
        with tempfile.TemporaryDirectory(prefix="coreguard-direct-", dir=BUILD) as temp_dir:
            report = pathlib.Path(temp_dir) / "target.json"
            resolved_target_args = [
                str(report) if argument == "target.json" else argument
                for argument in target_args
            ]
            command = [
                str(COREGUARD),
                "run",
                "--json",
                *coreguard_args,
                "--",
                sys.executable,
                str(PROBE),
                *resolved_target_args,
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            payload = self.parse_json_output(completed.stdout, completed.stderr)
            target = json.loads(report.read_text(encoding="utf-8"))
            return payload, target, completed.stderr

    def run_outer(
        self,
        outer_mode: str,
        target_mode: str,
        *,
        timeout_ms: int = 5000,
        inner_memory_mb: int | None = None,
        inner_cpu_ms: int | None = None,
        inner_max_processes: int | None = None,
        outer_active_processes: int | None = None,
        target_value: int | None = None,
    ) -> dict:
        with tempfile.TemporaryDirectory(prefix="coreguard-outer-", dir=BUILD) as temp_dir:
            report = pathlib.Path(temp_dir) / "outer.json"
            target_report = pathlib.Path(temp_dir) / "target.json"
            command = [
                sys.executable,
                str(PROBE),
                "--outer",
                "--coreguard",
                str(COREGUARD),
                "--report",
                str(report),
                "--target-report",
                str(target_report),
                "--outer-mode",
                outer_mode,
                "--target-mode",
                target_mode,
                "--timeout-ms",
                str(timeout_ms),
            ]
            if inner_memory_mb is not None:
                command.extend(["--inner-memory-mb", str(inner_memory_mb)])
            if inner_cpu_ms is not None:
                command.extend(["--inner-cpu-ms", str(inner_cpu_ms)])
            if inner_max_processes is not None:
                command.extend(["--inner-max-processes", str(inner_max_processes)])
            if outer_active_processes is not None:
                command.extend(["--outer-active-processes", str(outer_active_processes)])
            if target_value is not None:
                command.extend(["--target-value", str(target_value)])
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(report.read_text(encoding="utf-8"))

    @staticmethod
    def parse_json_output(stdout: str, stderr: str) -> dict:
        try:
            value = json.loads(stdout.strip())
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
        for line in reversed([line for line in stdout.splitlines() if line.strip()]):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise AssertionError(f"Coreguard emitted no JSON result. stdout={stdout!r} stderr={stderr!r}")

    def assert_nested_metrics_shape(self, payload: dict) -> None:
        metrics = payload["job_metrics"]
        for field in (
            "total_processes",
            "active_processes",
            "total_user_cpu_ms",
            "total_kernel_cpu_ms",
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
            "total_page_faults",
            "peak_job_memory_used_bytes",
        ):
            self.assertIn(field, metrics)
            self.assertGreaterEqual(metrics[field], 0)

    def test_explicit_breakaway_is_prevented_before_creation(self) -> None:
        payload, target, _ = self.run_coreguard(["--breakaway", "target.json"])
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(target["create_success"])
        self.assertIsNone(target["child_pid"])
        self.assertIsNotNone(target["win32_error"])
        self.assertGreater(target["win32_error"], 0)
        self.assertEqual(payload["job_metrics"]["total_processes"], 1)

    def test_pid_list_oracle_covers_normal_tree(self) -> None:
        payload, target, _ = self.run_coreguard(["--oracle", "target.json"])
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(set(target["expected_pids"]), set(target["job_pids"]))
        self.assertEqual(target["assigned_processes"], 3)
        self.assertEqual(payload["job_metrics"]["total_processes"], 3)

    def test_nested_child_job_stays_in_coreguard_hierarchy(self) -> None:
        payload, target, _ = self.run_coreguard(["--nested-normal", "target.json"])
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(set(target["expected_pids"]), set(target["child_job_pids"]))
        self.assertTrue(target["root_in_child_job"])
        self.assertTrue(target["child_in_child_job"])
        self.assertTrue(target["grandchild_in_child_job"])
        self.assert_nested_metrics_shape(payload)
        self.assertEqual(payload["job_metrics"]["total_processes"], 3)

    def test_nested_timeout_terminates_entire_tree(self) -> None:
        payload, target, _ = self.run_coreguard(
            ["--nested-hold", "target.json"], ["--timeout-ms", "300"]
        )
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertTrue(payload["cleanup_ok"])
        for pid in target["child_job_pids"]:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and process_is_active(pid):
                time.sleep(0.02)
            self.assertFalse(process_is_active(pid), f"nested PID survived cleanup: {pid}")

    def test_nested_active_process_limit(self) -> None:
        payload, target, _ = self.run_coreguard(
            ["--nested-normal", "target.json"], ["--max-processes", "2"]
        )
        self.assertEqual(payload["status"], "resource_limit")
        self.assertEqual(payload["resource_limit_kind"], "active_processes")
        self.assertTrue(payload["cleanup_ok"])
        self.assertEqual(payload["job_metrics"]["total_processes"], 2)
        self.assertLessEqual(target["assigned_processes"], 2)

    def test_nested_memory_limit(self) -> None:
        payload, _, _ = self.run_coreguard(
            ["--nested-memory", "target.json", "40"], ["--memory-limit-mb", "64"]
        )
        self.assertEqual(payload["status"], "resource_limit")
        self.assertEqual(payload["resource_limit_kind"], "memory")
        self.assertTrue(payload["cleanup_ok"])
        self.assertGreaterEqual(payload["job_metrics"]["total_processes"], 2)
        self.assertGreater(payload["job_metrics"]["peak_job_memory_used_bytes"], 0)

    def test_nested_cpu_limit(self) -> None:
        payload, _, _ = self.run_coreguard(
            ["--nested-cpu", "target.json", "5"], ["--cpu-time-limit-ms", "250"]
        )
        self.assertEqual(payload["status"], "resource_limit")
        self.assertEqual(payload["resource_limit_kind"], "cpu_time")
        self.assertTrue(payload["cleanup_ok"])
        self.assertGreaterEqual(payload["job_metrics"]["total_user_cpu_ms"], 250)

    def test_plain_outer_job_and_permissive_outer_breakaway(self) -> None:
        plain = self.run_outer("plain", "nested-normal")
        self.assertEqual(plain["coreguard_payload"]["status"], "exited")
        self.assertIn(plain["coreguard_pid"], plain["outer_job_pids"])
        self.assertEqual(
            set(plain["target_report"]["child_job_pids"]),
            set(plain["target_report"]["expected_pids"]),
        )

        permissive = self.run_outer("breakaway", "breakaway")
        self.assertEqual(permissive["coreguard_payload"]["status"], "exited")
        self.assertFalse(permissive["target_report"]["create_success"])
        self.assertIsNotNone(permissive["target_report"]["win32_error"])

    def test_outer_active_limit_is_distinguished_from_inner_limit(self) -> None:
        foreign = self.run_outer(
            "active",
            "nested-normal",
            inner_max_processes=10,
            outer_active_processes=2,
        )
        self.assertEqual(foreign["coreguard_payload"]["status"], "exited")
        self.assertFalse(foreign["coreguard_payload"]["resource_limit_hit"])
        self.assertEqual(foreign["target_report"]["grandchild_pid"], None)
        self.assertIsNotNone(foreign["target_report"]["worker_error"])

        inner = self.run_outer(
            "active",
            "nested-normal",
            inner_max_processes=2,
            outer_active_processes=10,
        )
        self.assertEqual(inner["coreguard_payload"]["status"], "resource_limit")
        self.assertEqual(inner["coreguard_payload"]["resource_limit_kind"], "active_processes")

    def test_outer_memory_limit_does_not_get_attributed_to_inner_job(self) -> None:
        result = self.run_outer(
            "memory",
            "nested-memory-catch",
            timeout_ms=1000,
            inner_memory_mb=256,
            target_value=40,
        )
        payload = result["coreguard_payload"]
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(payload["resource_limit_hit"])
        self.assertIsNone(payload["resource_limit_kind"])
        self.assertEqual(
            set(result["target_report"]["expected_pids"]),
            set(result["target_report"]["child_job_pids"]),
        )

    def test_kill_on_outer_job_close_cascades_to_nested_tree(self) -> None:
        result = self.run_outer("kill", "nested-hold", timeout_ms=10000)
        self.assertTrue(result["outer_closed"])
        self.assertIsNotNone(result["target_report"])
        for pid in result["target_report"]["child_job_pids"]:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and process_is_active(pid):
                time.sleep(0.02)
            self.assertFalse(process_is_active(pid), f"outer-close orphan survived: {pid}")

    def test_outer_ui_restriction_does_not_break_non_ui_nested_job_on_host(self) -> None:
        result = self.run_outer("ui", "nested-normal")
        self.assertEqual(result["coreguard_payload"]["status"], "exited")
        self.assertEqual(
            set(result["target_report"]["expected_pids"]),
            set(result["target_report"]["child_job_pids"]),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
