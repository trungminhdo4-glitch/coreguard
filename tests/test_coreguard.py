"""End-to-end acceptance tests for the Coreguard Windows CLI."""

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
HELPER = ROOT / "tests" / "helpers" / "tree_child.py"
RESOURCE_HELPER = ROOT / "tests" / "helpers" / "resource_tree.py"
CPU_HELPER = ROOT / "tests" / "helpers" / "cpu_tree.py"
ACTIVE_HELPER = ROOT / "tests" / "helpers" / "active_process_tree.py"
JOB_METRICS_HELPER = ROOT / "tests" / "helpers" / "job_metrics_tree.py"
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


def process_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        return bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == STILL_ACTIVE
        )
    finally:
        kernel32.CloseHandle(handle)


class CoreguardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Wave 4 targets Windows")
        configured_exe = os.environ.get("COREGUARD_EXE")
        candidates = (BUILD / "coreguard.exe", BUILD / "Release" / "coreguard.exe")
        cls.exe = pathlib.Path(configured_exe).resolve() if configured_exe else next(
            (candidate.resolve() for candidate in candidates if candidate.is_file()),
            candidates[0].resolve(),
        )
        if not cls.exe.is_file():
            raise unittest.SkipTest("build coreguard.exe first")

    def run_json(
        self,
        timeout_ms: int,
        command: list[str],
        memory_limit_mb: int | None = None,
        cpu_time_limit_ms: int | None = None,
        max_processes: int | None = None,
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        runner = [str(self.exe), "run", "--json", "--timeout-ms", str(timeout_ms)]
        if memory_limit_mb is not None:
            runner.extend(["--memory-limit-mb", str(memory_limit_mb)])
        if cpu_time_limit_ms is not None:
            runner.extend(["--cpu-time-limit-ms", str(cpu_time_limit_ms)])
        if max_processes is not None:
            runner.extend(["--max-processes", str(max_processes)])
        runner.extend(["--", *command])
        completed = subprocess.run(
            runner,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.stderr, "", completed.stderr)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail("invalid JSON: %s; stdout=%r" % (exc, completed.stdout))
        self.assertEqual(payload["metrics_scope"], "process")
        self.assertEqual(payload["job_metrics_scope"], "job")
        self.assertIn("resource_limit_hit", payload)
        self.assertIn("resource_limit_kind", payload)
        self.assertIsInstance(payload["metrics"], dict)
        self.assertIsInstance(payload["job_metrics"], dict)
        self.assertEqual(
            set(payload["metrics"]),
            {
                "creation_time_unix_100ns",
                "user_cpu_ms",
                "kernel_cpu_ms",
                "total_cpu_ms",
                "peak_working_set_bytes",
                "read_operations",
                "write_operations",
                "read_bytes",
                "write_bytes",
            },
        )
        self.assertEqual(
            set(payload["job_metrics"]),
            {
                "snapshot_available",
                "valid_fields",
                "total_user_cpu_ms",
                "total_kernel_cpu_ms",
                "total_page_faults",
                "total_processes",
                "active_processes",
                "total_terminated_processes",
                "read_operations",
                "write_operations",
                "other_operations",
                "read_bytes",
                "write_bytes",
                "other_bytes",
                "peak_job_memory_used_bytes",
                "query_error",
            },
        )
        return payload, completed

    def run_cli_option(
        self, option: str, value: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                option,
                value,
                "--",
                "definitely-not-a-real-coreguard-executable.exe",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )

    @staticmethod
    def read_report(path: pathlib.Path) -> dict:
        deadline = time.monotonic() + 3
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not path.is_file():
            raise AssertionError("fixture did not publish %s" % path)
        return json.loads(path.read_text(encoding="utf-8"))

    def job_metric(self, payload: dict, name: str) -> int:
        value = payload["job_metrics"][name]
        self.assertIsInstance(value, int, name)
        self.assertGreaterEqual(value, 0, name)
        return value

    def test_normal_exit(self) -> None:
        payload, completed = self.run_json(5000, ["cmd.exe", "/d", "/c", "exit 0"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(payload["exit_code"], 0)
        self.assertFalse(payload["timed_out"])
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_process_identity_and_cpu_metrics(self) -> None:
        code = (
            "value = 0\n"
            "for index in range(8000000):\n"
            "    value = (value + index) & 0xffffffff\n"
        )
        payload, completed = self.run_json(5000, [sys.executable, "-c", code])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        metrics = payload["metrics"]
        self.assertGreater(payload["process_id"], 0)
        self.assertIsInstance(metrics["creation_time_unix_100ns"], int)
        self.assertGreater(metrics["creation_time_unix_100ns"], 0)
        for key in ("user_cpu_ms", "kernel_cpu_ms", "total_cpu_ms"):
            self.assertIsInstance(metrics[key], int)
            self.assertGreaterEqual(metrics[key], 0)
        self.assertEqual(
            metrics["total_cpu_ms"],
            metrics["user_cpu_ms"] + metrics["kernel_cpu_ms"],
        )

    def test_peak_working_set_is_plausible(self) -> None:
        baseline, baseline_completed = self.run_json(
            5000, [sys.executable, "-c", "pass"]
        )
        self.assertEqual(baseline_completed.returncode, 0)
        code = (
            "data = bytearray(16 * 1024 * 1024)\n"
            "for index in range(0, len(data), 4096):\n"
            "    data[index] = 1\n"
        )
        allocated, allocated_completed = self.run_json(
            5000, [sys.executable, "-c", code]
        )
        self.assertEqual(allocated_completed.returncode, 0)
        self.assertGreaterEqual(baseline["metrics"]["peak_working_set_bytes"], 0)
        self.assertGreater(allocated["metrics"]["peak_working_set_bytes"], 0)
        self.assertGreater(
            allocated["metrics"]["peak_working_set_bytes"],
            baseline["metrics"]["peak_working_set_bytes"],
        )

    def test_io_metrics_are_available_for_controlled_file_io(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-metrics-io-", dir=str(ROOT / "build")
        ) as temporary:
            path = pathlib.Path(temporary) / "payload.bin"
            code = (
                "from pathlib import Path\n"
                "import sys\n"
                "path = Path(sys.argv[1])\n"
                "data = b'x' * (1024 * 1024)\n"
                "path.write_bytes(data)\n"
                "path.read_bytes()\n"
            )
            payload, completed = self.run_json(
                5000, [sys.executable, "-c", code, str(path)]
            )
        self.assertEqual(completed.returncode, 0)
        metrics = payload["metrics"]
        for key in (
            "read_operations",
            "write_operations",
            "read_bytes",
            "write_bytes",
        ):
            self.assertIsInstance(metrics[key], int)
            self.assertGreaterEqual(metrics[key], 0)

    def test_nonzero_exit_is_not_infrastructure_failure(self) -> None:
        payload, completed = self.run_json(5000, ["cmd.exe", "/d", "/c", "exit 7"])
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(payload["exit_code"], 7)
        self.assertFalse(payload["timed_out"])
        self.assertIsNotNone(payload["metrics"]["creation_time_unix_100ns"])

    def test_timeout_is_classified_and_returns(self) -> None:
        payload, completed = self.run_json(
            250, [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertTrue(payload["cleanup_ok"])
        self.assertIsNone(payload["exit_code"])
        self.assertLess(payload["duration_ms"], 6000)
        self.assertIsNotNone(payload["metrics"]["creation_time_unix_100ns"])

    def test_active_process_limit_one_counts_root_and_classifies_handled_spawn(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-one-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(ACTIVE_HELPER), "--helper", "attempt-child", str(report_path)],
                max_processes=1,
            )
            report = self.read_report(report_path)
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "active_processes")
            self.assertTrue(payload["resource_limit_hit"])
            self.assertTrue(payload["cleanup_ok"])
            self.assertTrue(payload["job_metrics"]["snapshot_available"])
            self.assertGreaterEqual(self.job_metric(payload, "total_processes"), 1)
            self.assertEqual(self.job_metric(payload, "active_processes"), 0)
            self.assertEqual(report["root_exit_code"], 0)
            self.assertFalse(report["attempts"][0]["ok"])
            self.assertEqual(report["attempts"][0]["winerror"], 1816)

    def test_active_process_limit_takes_precedence_over_timeout(self) -> None:
        timeout_ms = 300
        code = (
            "import subprocess, sys, time\n"
            "print('root-live', flush=True)\n"
            "time.sleep(0.1)\n"
            "try:\n"
            "    subprocess.Popen([sys.executable, '-c', 'pass'])\n"
            "except OSError as exc:\n"
            "    if exc.winerror != 1816:\n"
            "        raise\n"
            "    time.sleep(5)\n"
            "else:\n"
            "    raise SystemExit('spawn unexpectedly succeeded')\n"
            "print('root-finished', flush=True)\n"
        )
        payload, completed = self.run_json(
            timeout_ms,
            [sys.executable, "-c", code],
            max_processes=1,
        )
        self.assertEqual(completed.returncode, 123)
        self.assertEqual(payload["status"], "resource_limit")
        self.assertEqual(payload["resource_limit_kind"], "active_processes")
        self.assertTrue(payload["resource_limit_hit"])
        self.assertFalse(payload["timed_out"])
        self.assertTrue(payload["cleanup_ok"])
        self.assertIn("root-live\n", payload["stdout"])
        self.assertNotIn("root-finished", payload["stdout"])
        self.assertGreaterEqual(payload["duration_ms"], timeout_ms)

    def test_active_process_limit_two_allows_one_parallel_child(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-two-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(ACTIVE_HELPER),
                    "--helper",
                    "parallel",
                    str(report_path),
                    "2",
                ],
                max_processes=2,
            )
            report = self.read_report(report_path)
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["resource_limit_kind"], "active_processes")
            self.assertTrue(payload["cleanup_ok"])
            self.assertEqual(sum(attempt["ok"] for attempt in report["attempts"]), 1)
            self.assertEqual(
                [attempt["winerror"] for attempt in report["attempts"] if not attempt["ok"]],
                [1816],
            )

    def test_active_process_limit_sequential_reuse_is_not_lifetime_limit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-reuse-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(ACTIVE_HELPER), "--helper", "sequential", str(report_path)],
                max_processes=2,
            )
            report = self.read_report(report_path)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(payload["status"], "exited")
            self.assertFalse(payload["resource_limit_hit"])
            self.assertTrue(payload["cleanup_ok"])
            self.assertEqual([attempt["ok"] for attempt in report["attempts"]], [True, True])

    def test_active_process_limit_covers_grandchild(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-grandchild-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(ACTIVE_HELPER), "--helper", "grandchild", str(report_path)],
                max_processes=2,
            )
            nested_report = pathlib.Path(str(report_path) + ".nested")
            nested = self.read_report(nested_report)
            self.read_report(report_path)
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "active_processes")
            self.assertTrue(payload["cleanup_ok"])
            self.assertFalse(nested["attempts"][0]["ok"])
            self.assertEqual(nested["attempts"][0]["winerror"], 1816)

    def test_active_process_limit_bounds_parallel_spawn_explosion(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-explosion-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(ACTIVE_HELPER),
                    "--helper",
                    "explosion",
                    str(report_path),
                    "3",
                ],
                max_processes=4,
            )
            report = self.read_report(report_path)
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["resource_limit_kind"], "active_processes")
            self.assertTrue(payload["cleanup_ok"])
            self.assertEqual(len(report["fanout_reports"]), 3)
            for fanout_report in report["fanout_reports"]:
                child_report = self.read_report(pathlib.Path(fanout_report))
                self.assertTrue(child_report["attempts"])
                self.assertTrue(
                    all(
                        not attempt["ok"] and attempt["winerror"] == 1816
                        for attempt in child_report["attempts"]
                    )
                )

    def test_memory_limit_allows_clearly_below_limit_exit(self) -> None:
        code = (
            "data = bytearray(4 * 1024 * 1024)\n"
            "for index in range(0, len(data), 4096):\n"
            "    data[index] = 1\n"
        )
        payload, completed = self.run_json(
            5000, [sys.executable, "-c", code], memory_limit_mb=128
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(payload["resource_limit_hit"])
        self.assertTrue(payload["cleanup_ok"])

    def test_active_process_limit_does_not_change_memory_cause(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-memory-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(RESOURCE_HELPER), "--spawn-child-catch", str(pid_file), "256"],
                memory_limit_mb=64,
                max_processes=4,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "memory")
            self.assertTrue(payload["cleanup_ok"])

    def test_cpu_limit_allows_clearly_below_limit_exit(self) -> None:
        payload, completed = self.run_json(
            5000,
            [sys.executable, str(CPU_HELPER), "--burn", "0.05"],
            cpu_time_limit_ms=500,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(payload["resource_limit_hit"])
        self.assertIsNone(payload["resource_limit_kind"])
        self.assertTrue(payload["cleanup_ok"])

    def test_active_process_limit_does_not_change_cpu_cause(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-active-cpu-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                10000,
                [sys.executable, str(CPU_HELPER), "--spawn-child", str(pid_file), "5"],
                cpu_time_limit_ms=200,
                max_processes=4,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "cpu_time")
            self.assertTrue(payload["cleanup_ok"])

    def test_all_resource_limits_accept_normal_exit(self) -> None:
        payload, completed = self.run_json(
            5000,
            ["cmd.exe", "/d", "/c", "exit 0"],
            memory_limit_mb=128,
            cpu_time_limit_ms=2000,
            max_processes=4,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(payload["resource_limit_hit"])
        self.assertTrue(payload["cleanup_ok"])

    def test_active_process_limit_keeps_timeout_distinct(self) -> None:
        payload, completed = self.run_json(
            150,
            ["cmd.exe", "/d", "/c", "ping 127.0.0.1 -n 6 >nul"],
            max_processes=4,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertFalse(payload["resource_limit_hit"])
        self.assertTrue(payload["cleanup_ok"])

    def test_cpu_limit_classifies_root_enforcement(self) -> None:
        payload, completed = self.run_json(
            10000,
            [sys.executable, str(CPU_HELPER), "--burn", "5"],
            cpu_time_limit_ms=200,
        )
        self.assertEqual(completed.returncode, 123)
        self.assertEqual(payload["status"], "resource_limit")
        self.assertTrue(payload["resource_limit_hit"])
        self.assertEqual(payload["resource_limit_kind"], "cpu_time")
        self.assertFalse(payload["timed_out"])
        self.assertTrue(payload["cleanup_ok"])
        self.assertGreater(self.job_metric(payload, "total_user_cpu_ms"), 0)
        self.assertGreaterEqual(
            self.job_metric(payload, "total_user_cpu_ms"),
            payload["metrics"]["user_cpu_ms"],
        )

    def test_cpu_limit_is_not_wall_clock_timeout(self) -> None:
        payload, completed = self.run_json(
            1500,
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cpu_time_limit_ms=50,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertFalse(payload["resource_limit_hit"])
        self.assertGreaterEqual(payload["duration_ms"], 900)

    def test_cpu_limit_classifies_child_and_cleans_tree(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-cpu-child-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                10000,
                [sys.executable, str(CPU_HELPER), "--spawn-child", str(pid_file), "5"],
                cpu_time_limit_ms=200,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "cpu_time")
            self.assertTrue(payload["cleanup_ok"])
            deadline = time.monotonic() + 3
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "CPU child did not publish PIDs")
            pids = [int(value) for value in pid_file.read_text(encoding="ascii").split()]
            for pid in pids:
                while process_is_active(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_active(pid), "PID %d survived CPU limit" % pid)

    def test_cpu_limit_classifies_grandchild_and_cleans_tree(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-cpu-grandchild-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                10000,
                [sys.executable, str(CPU_HELPER), "--tree", str(pid_file), "5"],
                cpu_time_limit_ms=200,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertEqual(payload["resource_limit_kind"], "cpu_time")
            self.assertTrue(payload["cleanup_ok"])
            deadline = time.monotonic() + 3
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "CPU grandchild did not publish PIDs")
            pids = [int(value) for value in pid_file.read_text(encoding="ascii").split()]
            self.assertEqual(len(pids), 3)
            for pid in pids:
                while process_is_active(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_active(pid), "PID %d survived CPU limit" % pid)

    def test_cpu_and_memory_limits_keep_native_cause_distinct(self) -> None:
        cpu_payload, cpu_completed = self.run_json(
            10000,
            [sys.executable, str(CPU_HELPER), "--burn", "5"],
            memory_limit_mb=128,
            cpu_time_limit_ms=200,
        )
        self.assertEqual(cpu_completed.returncode, 123)
        self.assertEqual(cpu_payload["status"], "resource_limit")
        self.assertEqual(cpu_payload["resource_limit_kind"], "cpu_time")
        memory_code = (
            "data = bytearray(256 * 1024 * 1024)\n"
            "for index in range(0, len(data), 4096):\n"
            "    data[index] = 1\n"
        )
        memory_payload, memory_completed = self.run_json(
            10000,
            [sys.executable, "-c", memory_code],
            memory_limit_mb=64,
            cpu_time_limit_ms=5000,
        )
        self.assertEqual(memory_completed.returncode, 123)
        self.assertEqual(memory_payload["status"], "resource_limit")
        self.assertEqual(memory_payload["resource_limit_kind"], "memory")

    def test_memory_limit_classifies_root_enforcement(self) -> None:
        code = (
            "data = bytearray(256 * 1024 * 1024)\n"
            "for index in range(0, len(data), 4096):\n"
            "    data[index] = 1\n"
            "import time; time.sleep(10)\n"
        )
        payload, completed = self.run_json(
            5000, [sys.executable, "-c", code], memory_limit_mb=64
        )
        self.assertEqual(completed.returncode, 123)
        self.assertEqual(payload["status"], "resource_limit")
        self.assertTrue(payload["resource_limit_hit"])
        self.assertFalse(payload["timed_out"])
        self.assertTrue(payload["cleanup_ok"])

    def test_memory_limit_classifies_child_and_cleans_tree(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-resource-child-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(RESOURCE_HELPER), "--spawn-child", str(pid_file), "256"],
                memory_limit_mb=64,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertTrue(payload["resource_limit_hit"])
            self.assertTrue(payload["cleanup_ok"])
            deadline = time.monotonic() + 3
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "resource child did not publish PIDs")
            pids = [int(value) for value in pid_file.read_text(encoding="ascii").split()]
            for pid in pids:
                while process_is_active(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_active(pid), "PID %d survived limit" % pid)

    def test_memory_limit_classifies_grandchild_and_cleans_tree(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-resource-grandchild-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(RESOURCE_HELPER), "--tree", str(pid_file), "256"],
                memory_limit_mb=64,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertTrue(payload["resource_limit_hit"])
            self.assertTrue(payload["cleanup_ok"])
            deadline = time.monotonic() + 3
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "resource grandchild did not publish PIDs")
            pids = [int(value) for value in pid_file.read_text(encoding="ascii").split()]
            self.assertEqual(len(pids), 3)
            for pid in pids:
                while process_is_active(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_active(pid), "PID %d survived limit" % pid)

    def test_memory_limit_race_keeps_timeout_and_child_failure_distinct(self) -> None:
        timeout_payload, timeout_completed = self.run_json(
            150,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            memory_limit_mb=128,
        )
        self.assertEqual(timeout_completed.returncode, 124)
        self.assertEqual(timeout_payload["status"], "timeout")
        self.assertFalse(timeout_payload["resource_limit_hit"])
        self.assertTrue(timeout_payload["cleanup_ok"])

        with tempfile.TemporaryDirectory(
            prefix="coreguard-resource-race-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(RESOURCE_HELPER),
                    "--spawn-child-catch",
                    str(pid_file),
                    "256",
                ],
                memory_limit_mb=64,
            )
            self.assertEqual(completed.returncode, 123)
            self.assertEqual(payload["status"], "resource_limit")
            self.assertTrue(payload["resource_limit_hit"])
            self.assertTrue(payload["cleanup_ok"])

    def test_timeout_parser_rejects_non_decimal_and_out_of_range_values(self) -> None:
        invalid_values = [
            "0",
            "+1",
            "-1",
            "-18446744073709551615",
            " -18446744073709551615",
            " 1",
            "1 ",
            "\t1",
            "1\t",
            "4294967296",
            "18446744073709551616",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                completed = self.run_cli_option("--timeout-ms", value)
                self.assertEqual(completed.returncode, 2)
                self.assertIn("invalid --timeout-ms", completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_timeout_parser_accepts_valid_boundaries(self) -> None:
        for value in ("1", "4294967295"):
            with self.subTest(value=value):
                completed = self.run_cli_option("--timeout-ms", value)
                self.assertEqual(completed.returncode, 125)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(json.loads(completed.stdout)["status"], "start_failed")

    def test_memory_parser_rejects_non_decimal_and_out_of_range_values(self) -> None:
        invalid_values = [
            "0",
            "+1",
            "-1",
            "-18446744073709551615",
            " -18446744073709551615",
            " 1",
            "1 ",
            "\t1",
            "1\t",
            "not-a-number",
            "17592186044416",
            "18446744073709551616",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                completed = self.run_cli_option("--memory-limit-mb", value)
                self.assertEqual(completed.returncode, 2)
                self.assertIn("invalid --memory-limit-mb", completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_memory_parser_accepts_valid_boundaries(self) -> None:
        for value in ("1", "17592186044415"):
            with self.subTest(value=value):
                completed = self.run_cli_option("--memory-limit-mb", value)
                self.assertEqual(completed.returncode, 125)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(json.loads(completed.stdout)["status"], "start_failed")

    def test_duplicate_memory_limit_fails_before_spawn(self) -> None:

        duplicate = subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                "--memory-limit-mb",
                "64",
                "--memory-limit-mb",
                "128",
                "--",
                "cmd.exe",
                "/d",
                "/c",
                "exit 0",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)

    def test_invalid_cpu_time_limit_values_fail_before_spawn(self) -> None:
        invalid_values = [
            "0",
            "-1",
            "not-a-number",
            "18446744073709551615",
            "922337203685478",
            "1.5",
            " 1",
            "+1",
        ]
        for value in invalid_values:
            completed = subprocess.run(
                [
                    str(self.exe),
                    "run",
                    "--json",
                    "--cpu-time-limit-ms",
                    value,
                    "--",
                    "cmd.exe",
                    "/d",
                    "/c",
                    "exit 0",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, value)
            self.assertIn("invalid --cpu-time-limit-ms", completed.stderr)
            self.assertEqual(completed.stdout, "")

        duplicate = subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                "--cpu-time-limit-ms",
                "200",
                "--cpu-time-limit-ms",
                "300",
                "--",
                "cmd.exe",
                "/d",
                "/c",
                "exit 0",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)

    def test_invalid_active_process_limit_values_fail_before_spawn(self) -> None:
        invalid_values = [
            "0",
            "-1",
            "abc",
            "1.5",
            "+1",
            " 1",
            "4294967296",
            "18446744073709551615",
        ]
        for value in invalid_values:
            completed = subprocess.run(
                [
                    str(self.exe),
                    "run",
                    "--json",
                    "--max-processes",
                    value,
                    "--",
                    "cmd.exe",
                    "/d",
                    "/c",
                    "exit 0",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, value)
            self.assertIn("invalid --max-processes", completed.stderr)
            self.assertEqual(completed.stdout, "")

        missing = subprocess.run(
            [str(self.exe), "run", "--json", "--max-processes"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

        duplicate = subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                "--max-processes",
                "2",
                "--max-processes",
                "3",
                "--",
                "cmd.exe",
                "/d",
                "/c",
                "exit 0",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(duplicate.stdout, "")

        valid_max = self.run_json(
            5000, ["cmd.exe", "/d", "/c", "exit 0"], max_processes=4294967295
        )
        self.assertEqual(valid_max[1].returncode, 0)
        self.assertEqual(valid_max[0]["status"], "exited")

    def test_cpu_limit_and_timeout_keep_sleeper_timeout_distinct(self) -> None:
        payload, completed = self.run_json(
            150,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cpu_time_limit_ms=5000,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["timed_out"])
        self.assertFalse(payload["resource_limit_hit"])
        self.assertIsNone(payload["resource_limit_kind"])
        self.assertTrue(payload["cleanup_ok"])

    def test_process_tree_timeout_cleans_parent_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-tree-", dir=str(ROOT / "build")
        ) as temporary:
            pid_file = pathlib.Path(temporary) / "pids.txt"
            payload, completed = self.run_json(
                500, [sys.executable, str(HELPER), str(pid_file)]
            )
            self.assertEqual(completed.returncode, 124)
            self.assertEqual(payload["status"], "timeout")
            self.assertTrue(payload["cleanup_ok"])
            deadline = time.monotonic() + 3
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "tree fixture did not publish PIDs")
            parent_pid, grandchild_pid = [
                int(value) for value in pid_file.read_text(encoding="ascii").split()
            ]
            for pid in (parent_pid, grandchild_pid):
                while process_is_active(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_is_active(pid), "PID %d survived timeout" % pid)

    def test_stdout_stderr_are_separate_and_json_safe(self) -> None:
        code = "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)"
        payload, completed = self.run_json(5000, [sys.executable, "-c", code])
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(payload["stdout"], "out\n")
        self.assertEqual(payload["stderr"], "err\n")

    def test_job_metrics_aggregate_cpu_and_parallel_children(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-cpu-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--parallel",
                    str(report_path),
                    "2",
                ],
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertGreaterEqual(self.job_metric(payload, "total_processes"), 3)
        self.assertEqual(self.job_metric(payload, "active_processes"), 0)
        self.assertGreater(
            self.job_metric(payload, "total_user_cpu_ms"),
            payload["metrics"]["total_cpu_ms"],
        )

    def test_job_metrics_count_sequential_children_as_lifetime(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-sequential-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--sequential",
                    str(report_path),
                    "3",
                ],
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertGreaterEqual(self.job_metric(payload, "total_processes"), 4)
        self.assertEqual(self.job_metric(payload, "active_processes"), 0)

    def test_job_metrics_include_grandchild(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-grandchild-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [sys.executable, str(JOB_METRICS_HELPER), "--grandchild", str(report_path)],
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertGreaterEqual(self.job_metric(payload, "total_processes"), 3)
        self.assertGreater(self.job_metric(payload, "total_user_cpu_ms"), 0)

    def test_job_metrics_aggregate_io_counters(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-io-", dir=str(ROOT / "build")
        ) as temporary:
            directory = pathlib.Path(temporary)
            report_path = directory / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--io",
                    str(report_path),
                    str(directory / "root.bin"),
                    str(directory / "child.bin"),
                ],
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertGreaterEqual(self.job_metric(payload, "read_bytes"), 1024 * 1024)
        self.assertGreaterEqual(self.job_metric(payload, "write_bytes"), 1024 * 1024)
        self.assertGreater(self.job_metric(payload, "read_operations"), 0)
        self.assertGreater(self.job_metric(payload, "write_operations"), 0)

    def test_job_metrics_peak_memory_is_job_wide(self) -> None:
        baseline, baseline_completed = self.run_json(
            5000, [sys.executable, "-c", "pass"]
        )
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-memory-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            allocated, allocated_completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--memory",
                    str(report_path),
                    "12",
                    "12",
                    "0.1",
                ],
            )
        self.assertEqual(baseline_completed.returncode, 0)
        self.assertEqual(allocated_completed.returncode, 0)
        self.assertGreater(
            self.job_metric(allocated, "peak_job_memory_used_bytes"),
            self.job_metric(baseline, "peak_job_memory_used_bytes"),
        )

    def test_job_metrics_keep_memory_limit_classification(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-memory-limit-", dir=str(ROOT / "build")
        ) as temporary:
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--memory",
                    str(pathlib.Path(temporary) / "report.json"),
                    "32",
                    "32",
                    "5",
                ],
                memory_limit_mb=64,
            )
        self.assertEqual(completed.returncode, 123)
        self.assertEqual(payload["status"], "resource_limit")
        self.assertEqual(payload["resource_limit_kind"], "memory")
        self.assertTrue(payload["job_metrics"]["snapshot_available"])
        self.assertGreater(self.job_metric(payload, "peak_job_memory_used_bytes"), 0)

    def test_job_metrics_keep_timeout_classification(self) -> None:
        payload, completed = self.run_json(
            150, [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["cleanup_ok"])
        self.assertTrue(payload["job_metrics"]["snapshot_available"])
        self.assertEqual(self.job_metric(payload, "active_processes"), 0)

    def test_job_metrics_snapshot_includes_still_active_child_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="coreguard-job-snapshot-", dir=str(ROOT / "build")
        ) as temporary:
            report_path = pathlib.Path(temporary) / "report.json"
            payload, completed = self.run_json(
                5000,
                [
                    sys.executable,
                    str(JOB_METRICS_HELPER),
                    "--orphan-child",
                    str(report_path),
                    "1.0",
                ],
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertTrue(payload["job_metrics"]["snapshot_available"])
        self.assertGreaterEqual(self.job_metric(payload, "total_processes"), 2)
        self.assertGreaterEqual(self.job_metric(payload, "active_processes"), 1)

    def test_argument_quoting_and_unicode(self) -> None:
        code = "import sys; print(repr(sys.argv[1:]))"
        args = ["two words", 'quote"inside', "ümlaut", ""]
        payload, completed = self.run_json(5000, [sys.executable, "-c", code, *args])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "exited")
        self.assertIn("two words", payload["stdout"])
        self.assertIn('quote"inside', payload["stdout"])
        self.assertIn("ümlaut", payload["stdout"])
        self.assertIn("''", payload["stdout"])

    def test_invalid_executable_is_start_failure(self) -> None:
        payload, completed = self.run_json(
            5000, ["definitely-not-a-real-coreguard-executable.exe"]
        )
        self.assertEqual(completed.returncode, 125)
        self.assertEqual(payload["status"], "start_failed")
        self.assertIsNone(payload["exit_code"])
        self.assertFalse(payload["timed_out"])
        self.assertIn("win32_error", payload)
        self.assertTrue(all(value is None for value in payload["metrics"].values()))
        self.assertFalse(payload["job_metrics"]["snapshot_available"])
        self.assertIsNone(payload["job_metrics"]["query_error"])
        self.assertTrue(
            all(
                payload["job_metrics"][name] is None
                for name in (
                    "total_user_cpu_ms",
                    "total_kernel_cpu_ms",
                    "total_page_faults",
                    "total_processes",
                    "active_processes",
                    "total_terminated_processes",
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                    "peak_job_memory_used_bytes",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
