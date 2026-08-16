"""Bounded verification runner for the standalone Windows core."""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time
from collections import Counter
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
TREE_HELPER = ROOT / "tests" / "helpers" / "tree_child.py"
RESOURCE_HELPER = ROOT / "tests" / "helpers" / "resource_tree.py"
CPU_HELPER = ROOT / "tests" / "helpers" / "cpu_tree.py"
ACTIVE_HELPER = ROOT / "tests" / "helpers" / "active_process_tree.py"
JOB_METRICS_HELPER = ROOT / "tests" / "helpers" / "job_metrics_tree.py"
ARGV_HELPER = ROOT / "tests" / "helpers" / "argv_oracle.py"
DEFAULT_EXE = BUILD / "coreguard.exe"
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
HANDLE_COUNT_TOLERANCE = 4
SEED = 0xC0DE03


class VerificationFailure(RuntimeError):
    """A reproducible verification assertion failed."""


def require_windows() -> None:
    if os.name != "nt":
        raise VerificationFailure("Coreguard verification requires Windows")


def parse_single_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    try:
        value, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise VerificationFailure("invalid JSON: %s" % exc) from exc
    if stripped[end:].strip():
        raise VerificationFailure("JSON output contained trailing data")
    return value


def run_coreguard(
    exe: pathlib.Path,
    timeout_ms: int,
    command: list[str],
    timeout_seconds: float = 20.0,
    memory_limit_mb: int | None = None,
    cpu_time_limit_ms: int | None = None,
    max_processes: int | None = None,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    runner = [str(exe), "run", "--json", "--timeout-ms", str(timeout_ms)]
    if memory_limit_mb is not None:
        runner.extend(["--memory-limit-mb", str(memory_limit_mb)])
    if cpu_time_limit_ms is not None:
        runner.extend(["--cpu-time-limit-ms", str(cpu_time_limit_ms)])
    if max_processes is not None:
        runner.extend(["--max-processes", str(max_processes)])
    runner.extend(["--", *command])
    try:
        completed = subprocess.run(
            runner,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationFailure("coreguard runner hung: %s" % exc) from exc
    if completed.stderr:
        raise VerificationFailure("coreguard polluted stderr: %r" % completed.stderr)
    payload = parse_single_json(completed.stdout)
    if not isinstance(payload, dict):
        raise VerificationFailure("coreguard JSON result is not an object")
    required = {
        "status",
        "exit_code",
        "timed_out",
        "resource_limit_hit",
        "resource_limit_kind",
        "duration_ms",
        "process_id",
        "cleanup_ok",
        "metrics_scope",
        "metrics",
        "output_truncated",
        "stdout",
        "stderr",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise VerificationFailure("coreguard JSON missing fields: %s" % missing)
    if payload["metrics_scope"] != "process" or not isinstance(
        payload["metrics"], dict
    ):
        raise VerificationFailure("process metrics scope/object is malformed")
    if payload.get("job_metrics_scope") != "job" or not isinstance(
        payload.get("job_metrics"), dict
    ):
        raise VerificationFailure("job metrics scope/object is malformed")
    expected_metrics = {
        "creation_time_unix_100ns",
        "user_cpu_ms",
        "kernel_cpu_ms",
        "total_cpu_ms",
        "peak_working_set_bytes",
        "read_operations",
        "write_operations",
        "read_bytes",
        "write_bytes",
    }
    if set(payload["metrics"]) != expected_metrics:
        raise VerificationFailure("process metrics fields are malformed")
    expected_job_metrics = {
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
    }
    if set(payload["job_metrics"]) != expected_job_metrics:
        raise VerificationFailure("job metrics fields are malformed")
    return payload, completed


def process_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
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


def current_process_handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    count = ctypes.c_uint32()
    if not kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(), ctypes.byref(count)
    ):
        raise VerificationFailure(
            "GetProcessHandleCount failed: %d" % ctypes.get_last_error()
        )
    return int(count.value)


def wait_for_pid_file(
    path: pathlib.Path, expected_count: int = 2, timeout_seconds: float = 3.0
) -> list[int]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "tree fixture did not publish %d PIDs" % expected_count
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                values = [
                    int(value) for value in path.read_text(encoding="ascii").split()
                ]
            except (OSError, ValueError) as exc:
                last_error = "tree fixture PID file was invalid: %s" % exc
            else:
                if len(values) == expected_count:
                    return values
                last_error = "tree fixture did not publish %d PIDs" % expected_count
        time.sleep(0.02)
    raise VerificationFailure(last_error)


def assert_pids_gone(pids: list[int], timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    for pid in pids:
        while process_is_active(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if process_is_active(pid):
            raise VerificationFailure("PID %d survived timeout" % pid)


def run_tree_timeout(exe: pathlib.Path, timeout_ms: int = 500) -> dict[str, Any]:
    BUILD.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coreguard-tree-", dir=str(BUILD)) as temp:
        pid_file = pathlib.Path(temp) / "pids.txt"
        payload, completed = run_coreguard(
            exe, timeout_ms, [sys.executable, str(TREE_HELPER), str(pid_file)]
        )
        if completed.returncode != 124 or payload["status"] != "timeout":
            raise VerificationFailure("tree timeout was not classified correctly")
        if not payload["cleanup_ok"]:
            raise VerificationFailure("tree timeout reported cleanup failure")
        pids = wait_for_pid_file(pid_file)
        assert_pids_gone(pids)
        return {"status": payload["status"], "pids_checked": len(pids)}


def run_resource_enforcement(exe: pathlib.Path) -> dict[str, Any]:
    cases = 0
    below_limit_code = (
        "data = bytearray(4 * 1024 * 1024)\n"
        "for index in range(0, len(data), 4096):\n"
        "    data[index] = 1\n"
    )
    below_payload, below_completed = run_coreguard(
        exe, 5000, [sys.executable, "-c", below_limit_code], memory_limit_mb=128
    )
    cases += 1
    if (
        below_completed.returncode != 0
        or below_payload["status"] != "exited"
        or below_payload["resource_limit_hit"]
    ):
        raise VerificationFailure("below-limit workload was misclassified")
    if below_payload["resource_limit_kind"] is not None:
        raise VerificationFailure("below-limit memory case fabricated a cause")

    root_limit_code = (
        "data = bytearray(256 * 1024 * 1024)\n"
        "for index in range(0, len(data), 4096):\n"
        "    data[index] = 1\n"
        "import time; time.sleep(10)\n"
    )
    root_payload, root_completed = run_coreguard(
        exe, 5000, [sys.executable, "-c", root_limit_code], memory_limit_mb=64
    )
    cases += 1
    if (
        root_completed.returncode != 123
        or root_payload["status"] != "resource_limit"
        or not root_payload["resource_limit_hit"]
        or not root_payload["cleanup_ok"]
    ):
        raise VerificationFailure("root memory limit was not enforced/classified")
    if root_payload["resource_limit_kind"] != "memory":
        raise VerificationFailure("root memory cause was not reported")

    near_boundary = []
    for allocated_mb in (8, 16, 24, 32, 48):
        near_boundary_code = (
            "data = bytearray(%d * 1024 * 1024)\n" % allocated_mb
            + "for index in range(0, len(data), 4096):\n"
            + "    data[index] = 1\n"
            + "import time; time.sleep(0.2)\n"
        )
        payload, completed = run_coreguard(
            exe,
            5000,
            [sys.executable, "-c", near_boundary_code],
            memory_limit_mb=64,
        )
        cases += 1
        if (
            completed.returncode not in (0, 123)
            or payload["status"] not in ("exited", "resource_limit")
            or payload["resource_limit_hit"]
            != (payload["status"] == "resource_limit")
            or not payload["cleanup_ok"]
        ):
            raise VerificationFailure(
                "near-boundary memory case was not bounded: %d MiB" % allocated_mb
            )
        near_boundary.append(
            {"allocated_mb": allocated_mb, "status": payload["status"]}
        )

    tree_results = []
    for mode, expected_count in (("--spawn-child", 2), ("--tree", 3)):
        BUILD.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="coreguard-resource-", dir=str(BUILD)
        ) as temp:
            pid_file = pathlib.Path(temp) / "pids.txt"
            payload, completed = run_coreguard(
                exe,
                5000,
                [sys.executable, str(RESOURCE_HELPER), mode, str(pid_file), "256"],
                memory_limit_mb=64,
            )
            cases += 1
            if (
                completed.returncode != 123
                or payload["status"] != "resource_limit"
                or not payload["resource_limit_hit"]
                or not payload["cleanup_ok"]
            ):
                raise VerificationFailure("resource tree case was not bounded")
            pids = wait_for_pid_file(pid_file, expected_count)
            assert_pids_gone(pids)
            tree_results.append({"mode": mode, "pids_checked": len(pids)})

    BUILD.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="coreguard-resource-catch-", dir=str(BUILD)
    ) as temp:
        pid_file = pathlib.Path(temp) / "pids.txt"
        caught_payload, caught_completed = run_coreguard(
            exe,
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
    cases += 1
    if (
        caught_completed.returncode != 123
        or caught_payload["status"] != "resource_limit"
        or not caught_payload["resource_limit_hit"]
    ):
        raise VerificationFailure("allocation failure was confused with child exit")

    timeout_payload, timeout_completed = run_coreguard(
        exe,
        150,
        [sys.executable, "-c", "import time; time.sleep(5)"],
        memory_limit_mb=128,
    )
    cases += 1
    if (
        timeout_completed.returncode != 124
        or timeout_payload["status"] != "timeout"
        or timeout_payload["resource_limit_hit"]
        or not timeout_payload["cleanup_ok"]
    ):
        raise VerificationFailure("timeout/resource race changed timeout semantics")

    return {
        "status": "PASS",
        "cases": cases,
        "scope": "job",
        "unit": "MiB CLI input, bytes in C policy",
        "near_boundary": near_boundary,
        "tree": tree_results,
        "classification": {
            "below_limit": below_payload["status"],
            "root": root_payload["status"],
            "allocation_failure": caught_payload["status"],
            "timeout_race": timeout_payload["status"],
        },
    }


def run_cpu_enforcement(exe: pathlib.Path) -> dict[str, Any]:
    cases = 0
    below_payload, below_completed = run_coreguard(
        exe,
        5000,
        [sys.executable, str(CPU_HELPER), "--burn", "0.05"],
        cpu_time_limit_ms=500,
    )
    cases += 1
    if (
        below_completed.returncode != 0
        or below_payload["status"] != "exited"
        or below_payload["resource_limit_hit"]
        or below_payload["resource_limit_kind"] is not None
    ):
        raise VerificationFailure("below-limit CPU workload was misclassified")

    root_payload, root_completed = run_coreguard(
        exe,
        10000,
        [sys.executable, str(CPU_HELPER), "--burn", "5"],
        cpu_time_limit_ms=200,
    )
    cases += 1
    if (
        root_completed.returncode != 123
        or root_payload["status"] != "resource_limit"
        or not root_payload["resource_limit_hit"]
        or root_payload["resource_limit_kind"] != "cpu_time"
        or not root_payload["cleanup_ok"]
    ):
        raise VerificationFailure("root CPU limit was not enforced/classified")

    sleeper_payload, sleeper_completed = run_coreguard(
        exe,
        1500,
        [sys.executable, "-c", "import time; time.sleep(1)"],
        cpu_time_limit_ms=50,
    )
    cases += 1
    if (
        sleeper_completed.returncode != 0
        or sleeper_payload["status"] != "exited"
        or sleeper_payload["resource_limit_hit"]
        or sleeper_payload["duration_ms"] < 900
    ):
        raise VerificationFailure("sleeper demonstrated wall-time CPU confusion")

    tree_results = []
    for mode, expected_count in (("--spawn-child", 2), ("--tree", 3)):
        BUILD.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="coreguard-cpu-", dir=str(BUILD)) as temp:
            pid_file = pathlib.Path(temp) / "pids.txt"
            payload, completed = run_coreguard(
                exe,
                10000,
                [sys.executable, str(CPU_HELPER), mode, str(pid_file), "5"],
                cpu_time_limit_ms=200,
            )
            cases += 1
            if (
                completed.returncode != 123
                or payload["status"] != "resource_limit"
                or payload["resource_limit_kind"] != "cpu_time"
                or not payload["cleanup_ok"]
                or payload["metrics_scope"] != "process"
            ):
                raise VerificationFailure("CPU process-tree case was not bounded")
            pids = wait_for_pid_file(pid_file, expected_count)
            assert_pids_gone(pids)
            tree_results.append({"mode": mode, "pids_checked": len(pids)})

    timeout_payload, timeout_completed = run_coreguard(
        exe,
        150,
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cpu_time_limit_ms=5000,
    )
    cases += 1
    if (
        timeout_completed.returncode != 124
        or timeout_payload["status"] != "timeout"
        or timeout_payload["resource_limit_hit"]
        or timeout_payload["resource_limit_kind"] is not None
        or not timeout_payload["cleanup_ok"]
    ):
        raise VerificationFailure("CPU/timeout sleeper race changed timeout semantics")

    memory_payload, memory_completed = run_coreguard(
        exe,
        10000,
        [
            sys.executable,
            "-c",
            "data=bytearray(256*1024*1024)\n"
            "for index in range(0,len(data),4096): data[index]=1\n",
        ],
        memory_limit_mb=64,
        cpu_time_limit_ms=5000,
    )
    cases += 1
    if (
        memory_completed.returncode != 123
        or memory_payload["status"] != "resource_limit"
        or memory_payload["resource_limit_kind"] != "memory"
        or not memory_payload["cleanup_ok"]
    ):
        raise VerificationFailure("memory+CPU policy changed memory classification")

    natural_exit = []
    for _ in range(10):
        payload, completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(CPU_HELPER), "--burn", "0.15"],
            cpu_time_limit_ms=200,
        )
        cases += 1
        if completed.returncode not in (0, 123) or payload["status"] not in (
            "exited",
            "resource_limit",
        ):
            raise VerificationFailure("CPU natural-exit boundary was malformed")
        if payload["status"] == "exited" and payload["resource_limit_hit"]:
            raise VerificationFailure("natural CPU exit fabricated resource evidence")
        if payload["status"] == "resource_limit" and payload["resource_limit_kind"] != "cpu_time":
            raise VerificationFailure("CPU boundary reported the wrong cause")
        if not payload["cleanup_ok"]:
            raise VerificationFailure("CPU natural-exit boundary leaked cleanup")
        natural_exit.append(payload["status"])

    return {
        "status": "PASS",
        "cases": cases,
        "scope": "job",
        "unit": "milliseconds public, Windows 100-nanosecond user-mode ticks",
        "root": root_payload["status"],
        "below_limit": below_payload["status"],
        "sleeper": sleeper_payload["status"],
        "tree": tree_results,
        "timeout_race": timeout_payload["status"],
        "memory_race": memory_payload["resource_limit_kind"],
        "natural_exit": Counter(natural_exit),
        "metrics_scope": root_payload["metrics_scope"],
    }


def read_fixture_json(path: pathlib.Path) -> dict[str, Any]:
    deadline = time.monotonic() + 3.0
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.is_file():
        raise VerificationFailure("fixture did not publish %s" % path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationFailure("fixture report was not an object: %s" % path)
    return value


def run_active_process_enforcement(exe: pathlib.Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="coreguard-active-", dir=str(BUILD)) as temporary:
        root_report = pathlib.Path(temporary) / "root.json"
        payload, completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(ACTIVE_HELPER), "--helper", "attempt-child", str(root_report)],
            max_processes=1,
        )
        report = read_fixture_json(root_report)
        if (
            completed.returncode != 123
            or payload["status"] != "resource_limit"
            or payload["resource_limit_kind"] != "active_processes"
            or not payload["cleanup_ok"]
            or report["root_exit_code"] != 0
            or report["attempts"][0].get("winerror") != 1816
        ):
            raise VerificationFailure("limit=1 did not preserve native child failure evidence")
        cases.append({"case": "root-counts", "status": payload["status"]})

        parallel_report = pathlib.Path(temporary) / "parallel.json"
        payload, completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(ACTIVE_HELPER),
                "--helper",
                "parallel",
                str(parallel_report),
                "2",
            ],
            max_processes=2,
        )
        report = read_fixture_json(parallel_report)
        if (
            completed.returncode != 123
            or payload["resource_limit_kind"] != "active_processes"
            or not payload["cleanup_ok"]
            or sum(attempt["ok"] for attempt in report["attempts"]) != 1
        ):
            raise VerificationFailure("parallel process limit did not allow exactly one child")
        cases.append({"case": "parallel", "status": payload["status"]})

        sequential_report = pathlib.Path(temporary) / "sequential.json"
        payload, completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(ACTIVE_HELPER), "--helper", "sequential", str(sequential_report)],
            max_processes=2,
        )
        report = read_fixture_json(sequential_report)
        if (
            completed.returncode != 0
            or payload["status"] != "exited"
            or payload["resource_limit_hit"]
            or report["attempts"] != [{"ok": True, "pid": report["attempts"][0]["pid"]}, {"ok": True, "pid": report["attempts"][1]["pid"]}]
        ):
            raise VerificationFailure("sequential active-process capacity was not reusable")
        cases.append({"case": "sequential-reuse", "status": payload["status"]})

        grandchild_report = pathlib.Path(temporary) / "grandchild.json"
        payload, completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(ACTIVE_HELPER), "--helper", "grandchild", str(grandchild_report)],
            max_processes=2,
        )
        nested = read_fixture_json(pathlib.Path(str(grandchild_report) + ".nested"))
        if (
            completed.returncode != 123
            or payload["resource_limit_kind"] != "active_processes"
            or not payload["cleanup_ok"]
            or nested["attempts"][0].get("winerror") != 1816
        ):
            raise VerificationFailure("grandchild bypassed the active-process limit")
        cases.append({"case": "grandchild", "status": payload["status"]})

        explosion_report = pathlib.Path(temporary) / "explosion.json"
        payload, completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(ACTIVE_HELPER),
                "--helper",
                "explosion",
                str(explosion_report),
                "3",
            ],
            max_processes=4,
        )
        report = read_fixture_json(explosion_report)
        fanout_reports = [read_fixture_json(pathlib.Path(path)) for path in report["fanout_reports"]]
        if (
            completed.returncode != 123
            or payload["resource_limit_kind"] != "active_processes"
            or not payload["cleanup_ok"]
            or not all(
                all(not attempt["ok"] and attempt.get("winerror") == 1816 for attempt in child["attempts"])
                for child in fanout_reports
            )
        ):
            raise VerificationFailure("bounded spawn explosion exceeded active-process policy")
        cases.append({"case": "spawn-explosion", "status": payload["status"]})

        payload, completed = run_coreguard(
            exe,
            150,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            max_processes=4,
        )
        if completed.returncode != 124 or payload["status"] != "timeout" or payload["resource_limit_hit"]:
            raise VerificationFailure("active-process policy changed timeout semantics")
        cases.append({"case": "timeout", "status": payload["status"]})

        payload, completed = run_coreguard(
            exe,
            5000,
            ["cmd.exe", "/d", "/c", "exit 0"],
            memory_limit_mb=128,
            cpu_time_limit_ms=2000,
            max_processes=4,
        )
        if completed.returncode != 0 or payload["status"] != "exited" or payload["resource_limit_hit"]:
            raise VerificationFailure("combined resource policy changed normal exit")
        cases.append({"case": "combined-normal-exit", "status": payload["status"]})

    return {"status": "PASS", "cases": cases, "native_child_error": 1816}


def corpus_cases() -> list[list[str]]:
    atoms = [
        "",
        "a b",
        "a\tb",
        '"',
        "\\",
        "\\\\",
        'a\\"b',
        "trailing\\",
        "日本語",
        "ä ö ü",
        "&",
        "|",
        ">",
        "%PATH%",
        "^",
        "<",
        "&&",
        "||",
    ]
    cases = [[value] for value in atoms]
    cases.extend(
        [
            ["", "", ""],
            ["a b", 'quote"inside', "trailing\\"],
            ["\\", 'a\\"b', "\\\\"],
            ["日本語", "ä ö ü", "%PATH%", "&", "|"],
            [" ", "\t", "  leading", "trailing  "],
            ["shell & pipe | redirect > percent %PATH%", "^", "<", ";"],
            ["x" * 5000, "tail\\", ""],
            ["a" * 1024, "b" * 1024, "c" * 1024],
        ]
    )
    return cases


def generated_cases(seed: int, count: int) -> list[list[str]]:
    rng = random.Random(seed)
    tokens = ["", "a", "b", " ", "\t", "\\", '"', "ä", "日", "&", "|", "%"]
    cases: list[list[str]] = []
    for _ in range(count):
        argument_count = rng.randint(0, 6)
        case = []
        for _ in range(argument_count):
            token_count = rng.randint(0, 8)
            case.append("".join(rng.choice(tokens) for _ in range(token_count)))
        cases.append(case)
    return cases


def observe_roundtrip(exe: pathlib.Path, args: list[str]) -> list[str]:
    payload, completed = run_coreguard(
        exe, 5000, [sys.executable, str(ARGV_HELPER), *args]
    )
    if completed.returncode != 0 or payload["status"] != "exited":
        raise VerificationFailure("argv helper did not exit normally")
    observed = parse_single_json(payload["stdout"])
    if not isinstance(observed, list) or not all(
        isinstance(value, str) for value in observed
    ):
        raise VerificationFailure("argv helper returned malformed JSON")
    return observed


def run_json_contract(exe: pathlib.Path) -> dict[str, Any]:
    cases = 0
    payload, completed = run_coreguard(
        exe,
        5000,
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
    )
    cases += 1
    if completed.returncode != 7 or payload["status"] != "exited":
        raise VerificationFailure("nonzero child exit changed protocol classification")
    if payload["stdout"] != "out\n" or payload["stderr"] != "err\n":
        raise VerificationFailure("stdout/stderr were not kept separate")
    payload, completed = run_coreguard(
        exe, 250, [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    cases += 1
    if completed.returncode != 124 or payload["status"] != "timeout":
        raise VerificationFailure("timeout changed protocol classification")
    payload, completed = run_coreguard(exe, 5000, ["definitely-not-a-coreguard-exe.exe"])
    cases += 1
    if completed.returncode != 125 or payload["status"] != "start_failed":
        raise VerificationFailure("spawn failure changed protocol classification")
    payload, completed = run_coreguard(
        exe,
        5000,
        [sys.executable, "-X", "utf8", "-c", "print('日本語 ä ö ü')"],
    )
    cases += 1
    if payload["stdout"] != "日本語 ä ö ü\n" or completed.returncode != 0:
        raise VerificationFailure("Unicode JSON output was not preserved")
    payload, completed = run_coreguard(
        exe,
        5000,
        [sys.executable, "-c", "print('x' * (1024 * 1024 + 4096), end='')"],
    )
    cases += 1
    if not payload["output_truncated"] or len(payload["stdout"]) > 1024 * 1024:
        raise VerificationFailure("bounded output contract failed")
    return {"status": "PASS", "cases": cases}


def require_metric(payload: dict[str, Any], name: str) -> int:
    value = payload["metrics"].get(name)
    if not isinstance(value, int) or value < 0:
        raise VerificationFailure("metric %s was unavailable or negative" % name)
    return value


def require_job_metric(payload: dict[str, Any], name: str) -> int:
    value = payload["job_metrics"].get(name)
    if not isinstance(value, int) or value < 0:
        raise VerificationFailure("job metric %s was unavailable or negative" % name)
    return value


def run_metrics_contract(exe: pathlib.Path) -> dict[str, Any]:
    payload, completed = run_coreguard(exe, 5000, [sys.executable, "-c", "pass"])
    if completed.returncode != 0 or payload["status"] != "exited":
        raise VerificationFailure("metrics natural-exit fixture failed")
    creation_time = require_metric(payload, "creation_time_unix_100ns")
    if creation_time == 0 or payload["process_id"] <= 0:
        raise VerificationFailure("process identity metrics were not populated")

    cpu_code = (
        "value = 0\n"
        "for index in range(8000000):\n"
        "    value = (value + index) & 0xffffffff\n"
    )
    cpu_payload, cpu_completed = run_coreguard(
        exe, 5000, [sys.executable, "-c", cpu_code]
    )
    if cpu_completed.returncode != 0 or cpu_payload["status"] != "exited":
        raise VerificationFailure("metrics CPU fixture failed")
    user_cpu = require_metric(cpu_payload, "user_cpu_ms")
    kernel_cpu = require_metric(cpu_payload, "kernel_cpu_ms")
    total_cpu = require_metric(cpu_payload, "total_cpu_ms")
    if total_cpu != user_cpu + kernel_cpu:
        raise VerificationFailure("total CPU time was not the component sum")

    memory_code = (
        "data = bytearray(16 * 1024 * 1024)\n"
        "for index in range(0, len(data), 4096):\n"
        "    data[index] = 1\n"
    )
    memory_payload, memory_completed = run_coreguard(
        exe, 5000, [sys.executable, "-c", memory_code]
    )
    if memory_completed.returncode != 0 or memory_payload["status"] != "exited":
        raise VerificationFailure("metrics memory fixture failed")
    peak_memory = require_metric(memory_payload, "peak_working_set_bytes")
    if peak_memory == 0:
        raise VerificationFailure("peak working set was zero for allocated fixture")

    BUILD.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coreguard-metrics-io-", dir=str(BUILD)) as temp:
        path = pathlib.Path(temp) / "payload.bin"
        io_code = (
            "from pathlib import Path\n"
            "import sys\n"
            "path = Path(sys.argv[1])\n"
            "data = b'x' * (1024 * 1024)\n"
            "path.write_bytes(data)\n"
            "path.read_bytes()\n"
        )
        io_payload, io_completed = run_coreguard(
            exe, 5000, [sys.executable, "-c", io_code, str(path)]
        )
    if io_completed.returncode != 0 or io_payload["status"] != "exited":
        raise VerificationFailure("metrics I/O fixture failed")
    io_values = {
        name: require_metric(io_payload, name)
        for name in (
            "read_operations",
            "write_operations",
            "read_bytes",
            "write_bytes",
        )
    }

    timeout_payload, timeout_completed = run_coreguard(
        exe, 250, [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    if timeout_completed.returncode != 124 or timeout_payload["status"] != "timeout":
        raise VerificationFailure("metrics timeout fixture changed timeout semantics")
    timeout_creation = require_metric(timeout_payload, "creation_time_unix_100ns")
    if timeout_creation == 0 or not timeout_payload["cleanup_ok"]:
        raise VerificationFailure("timeout metrics lacked identity or cleanup")

    return {
        "status": "PASS",
        "scope": "process",
        "creation_time_unix_100ns": creation_time,
        "cpu_ms": {
            "user": user_cpu,
            "kernel": kernel_cpu,
            "total": total_cpu,
        },
        "peak_working_set_bytes": peak_memory,
        "io": io_values,
        "timeout": {
            "status": timeout_payload["status"],
            "creation_time_unix_100ns": timeout_creation,
        },
    }


def run_job_metrics_contract(exe: pathlib.Path) -> dict[str, Any]:
    experiments: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="coreguard-job-metrics-", dir=str(BUILD)) as temp:
        directory = pathlib.Path(temp)

        cpu_report = directory / "cpu.json"
        cpu_payload, cpu_completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(JOB_METRICS_HELPER), "--parallel", str(cpu_report), "2"],
        )
        if cpu_completed.returncode != 0 or cpu_payload["status"] != "exited":
            raise VerificationFailure("job CPU aggregation fixture failed")
        if require_job_metric(cpu_payload, "total_processes") < 3:
            raise VerificationFailure("parallel CPU children were not counted")
        if require_job_metric(cpu_payload, "total_user_cpu_ms") <= require_metric(
            cpu_payload, "total_cpu_ms"
        ):
            raise VerificationFailure("job CPU did not exceed root-process CPU")
        experiments.append({"name": "cpu aggregation", "status": "PASS"})

        sequential_report = directory / "sequential.json"
        sequential_payload, sequential_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(JOB_METRICS_HELPER),
                "--sequential",
                str(sequential_report),
                "3",
            ],
        )
        if sequential_completed.returncode != 0 or sequential_payload["status"] != "exited":
            raise VerificationFailure("sequential child fixture failed")
        if require_job_metric(sequential_payload, "total_processes") < 4:
            raise VerificationFailure("sequential children were not lifetime-counted")
        if require_job_metric(sequential_payload, "active_processes") != 0:
            raise VerificationFailure("sequential child snapshot was not terminal")
        experiments.append({"name": "sequential children", "status": "PASS"})

        parallel_report = directory / "parallel.json"
        parallel_payload, parallel_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(JOB_METRICS_HELPER),
                "--parallel",
                str(parallel_report),
                "3",
            ],
        )
        if parallel_completed.returncode != 0 or parallel_payload["status"] != "exited":
            raise VerificationFailure("parallel child fixture failed")
        if require_job_metric(parallel_payload, "total_processes") < 4:
            raise VerificationFailure("parallel children were not counted")
        if require_job_metric(parallel_payload, "active_processes") != 0:
            raise VerificationFailure("parallel child snapshot was not terminal")
        experiments.append({"name": "parallel children", "status": "PASS"})

        grandchild_report = directory / "grandchild.json"
        grandchild_payload, grandchild_completed = run_coreguard(
            exe,
            5000,
            [sys.executable, str(JOB_METRICS_HELPER), "--grandchild", str(grandchild_report)],
        )
        if grandchild_completed.returncode != 0 or grandchild_payload["status"] != "exited":
            raise VerificationFailure("grandchild fixture failed")
        if require_job_metric(grandchild_payload, "total_processes") < 3:
            raise VerificationFailure("grandchild was not included in job accounting")
        experiments.append({"name": "grandchild", "status": "PASS"})

        io_report = directory / "io.json"
        io_payload, io_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(JOB_METRICS_HELPER),
                "--io",
                str(io_report),
                str(directory / "root.bin"),
                str(directory / "child.bin"),
            ],
        )
        if io_completed.returncode != 0 or io_payload["status"] != "exited":
            raise VerificationFailure("job I/O fixture failed")
        if require_job_metric(io_payload, "read_bytes") < 1024 * 1024:
            raise VerificationFailure("job read bytes missed the controlled lower bound")
        if require_job_metric(io_payload, "write_bytes") < 1024 * 1024:
            raise VerificationFailure("job write bytes missed the controlled lower bound")
        if require_job_metric(io_payload, "read_operations") == 0 or require_job_metric(
            io_payload, "write_operations"
        ) == 0:
            raise VerificationFailure("job I/O operation counters were zero")
        experiments.append({"name": "I/O", "status": "PASS"})

        baseline_payload, baseline_completed = run_coreguard(
            exe, 5000, [sys.executable, "-c", "pass"]
        )
        memory_report = directory / "memory.json"
        memory_payload, memory_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(JOB_METRICS_HELPER),
                "--memory",
                str(memory_report),
                "12",
                "12",
                "0.1",
            ],
        )
        if (
            baseline_completed.returncode != 0
            or memory_completed.returncode != 0
            or memory_payload["status"] != "exited"
            or require_job_metric(memory_payload, "peak_job_memory_used_bytes")
            <= require_job_metric(baseline_payload, "peak_job_memory_used_bytes")
        ):
            raise VerificationFailure("job peak memory did not react to child allocation")
        experiments.append({"name": "memory peak", "status": "PASS"})

        active_report = directory / "active.json"
        active_payload, active_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(ACTIVE_HELPER),
                "--helper",
                "attempt-child",
                str(active_report),
            ],
            max_processes=1,
        )
        if (
            active_completed.returncode != 123
            or active_payload["status"] != "resource_limit"
            or active_payload["resource_limit_kind"] != "active_processes"
            or require_job_metric(active_payload, "total_processes") < 1
        ):
            raise VerificationFailure("active-process accounting interaction was malformed")
        experiments.append({"name": "active-process limit interaction", "status": "PASS"})

        cpu_limit_report = directory / "cpu-limit.txt"
        cpu_limit_payload, cpu_limit_completed = run_coreguard(
            exe,
            10000,
            [sys.executable, str(CPU_HELPER), "--spawn-child", str(cpu_limit_report), "5"],
            cpu_time_limit_ms=200,
        )
        if (
            cpu_limit_completed.returncode != 123
            or cpu_limit_payload["status"] != "resource_limit"
            or cpu_limit_payload["resource_limit_kind"] != "cpu_time"
            or require_job_metric(cpu_limit_payload, "total_user_cpu_ms")
            < require_metric(cpu_limit_payload, "user_cpu_ms")
        ):
            raise VerificationFailure("CPU-limit metrics changed primary classification")
        experiments.append({"name": "CPU-limit interaction", "status": "PASS"})

        memory_limit_report = directory / "memory-limit.txt"
        memory_limit_payload, memory_limit_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(RESOURCE_HELPER),
                "--spawn-child",
                str(memory_limit_report),
                "256",
            ],
            memory_limit_mb=64,
        )
        if (
            memory_limit_completed.returncode != 123
            or memory_limit_payload["status"] != "resource_limit"
            or memory_limit_payload["resource_limit_kind"] != "memory"
            or require_job_metric(memory_limit_payload, "peak_job_memory_used_bytes") == 0
        ):
            raise VerificationFailure("memory-limit metrics changed primary classification")
        experiments.append({"name": "memory-limit interaction", "status": "PASS"})

        snapshot_report = directory / "snapshot.json"
        snapshot_payload, snapshot_completed = run_coreguard(
            exe,
            5000,
            [
                sys.executable,
                str(JOB_METRICS_HELPER),
                "--orphan-child",
                str(snapshot_report),
                "1.0",
            ],
        )
        if (
            snapshot_completed.returncode != 0
            or snapshot_payload["status"] != "exited"
            or require_job_metric(snapshot_payload, "total_processes") < 2
            or require_job_metric(snapshot_payload, "active_processes") < 1
        ):
            raise VerificationFailure("terminal snapshot lost an active descendant")
        experiments.append({"name": "active descendant snapshot", "status": "PASS"})

        timeout_payload, timeout_completed = run_coreguard(
            exe, 150, [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        if (
            timeout_completed.returncode != 124
            or timeout_payload["status"] != "timeout"
            or timeout_payload["resource_limit_hit"]
            or require_job_metric(timeout_payload, "active_processes") != 0
        ):
            raise VerificationFailure("timeout metrics changed primary classification")
        experiments.append({"name": "timeout interaction", "status": "PASS"})

    return {
        "status": "PASS",
        "scope": "job",
        "experiments": experiments,
        "metric_fields": "CG_JOB_METRIC_ALL",
    }


def run_roundtrip(exe: pathlib.Path, seed: int) -> dict[str, Any]:
    cases = corpus_cases() + generated_cases(seed, 100)
    for index, args in enumerate(cases):
        observed = observe_roundtrip(exe, args)
        if observed != args:
            raise VerificationFailure(
                "argv roundtrip mismatch at case %d: input=%r observed=%r"
                % (index, args, observed)
            )
    return {
        "status": "PASS",
        "corpus_cases": len(corpus_cases()),
        "property_cases": 100,
        "seed": seed,
        "total_cases": len(cases),
    }


def run_timeout_boundary(exe: pathlib.Path, timeout_ms: int) -> dict[str, Any]:
    if timeout_ms == 500:
        sleeps = [350, 450, 490, 500, 510, 550, 650]
        # 450 ms is intentionally boundary-zone on this Windows host: process
        # launch and scheduler jitter can consume the remaining 50 ms.
        clear_under = {350}
        clear_over = {550, 650}
    else:
        sleeps = [
            max(50, timeout_ms // 3),
            max(50, timeout_ms * 7 // 10),
            timeout_ms - 20,
            timeout_ms,
            timeout_ms + 20,
            timeout_ms + 300,
            timeout_ms + 600,
        ]
        clear_under = set(sleeps[:2])
        clear_over = set(sleeps[-2:])
    observations: list[dict[str, Any]] = []
    for sleep_ms in sleeps:
        payload, completed = run_coreguard(
            exe,
            timeout_ms,
            [
                sys.executable,
                "-c",
                "import time; time.sleep(%.3f)" % (sleep_ms / 1000),
            ],
            timeout_seconds=10,
        )
        if payload["duration_ms"] > 9000 or not payload["cleanup_ok"]:
            raise VerificationFailure("timeout boundary violated bounded cleanup")
        if sleep_ms in clear_under and payload["status"] != "exited":
            raise VerificationFailure("clear-under-boundary case timed out")
        if sleep_ms in clear_over and payload["status"] != "timeout":
            raise VerificationFailure("clear-over-boundary case did not time out")
        observations.append(
            {"sleep_ms": sleep_ms, "status": payload["status"], "duration_ms": payload["duration_ms"]}
        )
        if completed.returncode not in (0, 124):
            raise VerificationFailure("boundary returned unexpected CLI code")
    return {"status": "PASS", "timeout_ms": timeout_ms, "observations": observations}


def run_failure_injection(exe: pathlib.Path) -> dict[str, Any]:
    payload, completed = run_coreguard(exe, 5000, ["missing-coreguard-program.exe"])
    if payload["status"] != "start_failed" or payload["exit_code"] is not None:
        raise VerificationFailure("spawn failure fabricated a child exit code")
    race_payload, race_completed = run_coreguard(
        exe,
        50,
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
    )
    if race_completed.returncode not in (0, 124) or race_payload["status"] not in (
        "exited",
        "timeout",
    ):
        raise VerificationFailure("process-ended-during-timeout path was inconsistent")
    return {
        "status": "PASS",
        "tested": ["invalid executable", "process exit near timeout decision"],
        "untested": [
            "invalid working directory: CLI has no working-directory option",
            "pipe/output failure: capture uses bounded temporary files",
            "job assignment failure: not safely injectable without system mutation",
        ],
        "child_exit_codes": [completed.returncode, race_completed.returncode],
    }


def stress_case(exe: pathlib.Path, kind: str) -> None:
    if kind == "normal":
        payload, completed = run_coreguard(exe, 5000, ["cmd.exe", "/d", "/c", "exit 0"])
        if completed.returncode != 0 or payload["status"] != "exited":
            raise VerificationFailure("normal stress case failed")
    elif kind == "nonzero":
        payload, completed = run_coreguard(exe, 5000, ["cmd.exe", "/d", "/c", "exit 7"])
        if completed.returncode != 7 or payload["status"] != "exited":
            raise VerificationFailure("nonzero stress case was misclassified")
    elif kind == "spawn_failure":
        payload, completed = run_coreguard(exe, 5000, ["missing-stress-program.exe"])
        if completed.returncode != 125 or payload["status"] != "start_failed":
            raise VerificationFailure("spawn-failure stress case failed")
    elif kind == "timeout":
        payload, completed = run_coreguard(
            exe, 75, [sys.executable, "-c", "import time; time.sleep(2)"]
        )
        if completed.returncode != 124 or payload["status"] != "timeout":
            raise VerificationFailure("timeout stress case failed")
        if not payload["cleanup_ok"]:
            raise VerificationFailure("timeout stress case leaked cleanup")
    elif kind == "tree_timeout":
        run_tree_timeout(exe, 100)
    else:
        raise VerificationFailure("unknown stress case %s" % kind)


def run_handle_stress(exe: pathlib.Path) -> dict[str, Any]:
    sequence = (
        ["normal"] * 12
        + ["nonzero"] * 12
        + ["spawn_failure"] * 12
        + ["timeout"] * 12
        + ["tree_timeout"] * 8
    )
    before = current_process_handle_count()
    samples = [before]
    for index, kind in enumerate(sequence, start=1):
        stress_case(exe, kind)
        if index % 8 == 0:
            gc.collect()
            samples.append(current_process_handle_count())
    gc.collect()
    after = current_process_handle_count()
    samples.append(after)
    calibration_before = after
    stress_case(exe, "tree_timeout")
    gc.collect()
    calibration_after = current_process_handle_count()
    samples.append(calibration_after)
    positive_jumps = [
        right - left
        for left, right in zip(samples, samples[1:])
        if right - left > HANDLE_COUNT_TOLERANCE
    ]
    if calibration_after > calibration_before + HANDLE_COUNT_TOLERANCE:
        raise VerificationFailure(
            "parent handle count continued growing: samples=%r" % samples
        )
    if len(positive_jumps) > 1:
        raise VerificationFailure("parent handle count had repeated growth: %r" % samples)
    return {
        "status": "PASS",
        "iterations": len(sequence) + 1,
        "breakdown": dict(Counter(sequence + ["calibration_tree_timeout"])),
        "parent_handle_count_before": before,
        "parent_handle_count_after": after,
        "parent_handle_count_samples": samples,
        "one_time_baseline_shift": after - before,
        "measurement_limit": (
            "GetProcessHandleCount covers the Python verifier process only; "
            "per-type child/job/thread/pipe handles are not directly observable here."
        ),
    }


def run_differential(
    exe: pathlib.Path,
    agent_exec: pathlib.Path | None,
    seed: int,
) -> dict[str, Any]:
    cases = generated_cases(seed ^ 0xA5A5, 100)
    if agent_exec is None:
        return {
            "status": "NOT_APPLICABLE",
            "classification": {"OPTIONAL_ORACLE_NOT_CONFIGURED": len(cases)},
            "reason": "No external differential oracle was configured",
            "cases": len(cases),
            "executed_coreguard_cases": 0,
        }
    if not agent_exec.is_file():
        return {
            "status": "BLOCKED",
            "classification": {"ORACLE_INCONCLUSIVE": len(cases)},
            "reason": "agent-exec binary not found",
            "cases": len(cases),
            "executed_coreguard_cases": 0,
        }
    counts = Counter()
    mismatches: list[dict[str, Any]] = []
    for index, args in enumerate(cases):
        core_observed = observe_roundtrip(exe, args)
        try:
            completed = subprocess.run(
                [str(agent_exec), "--quiet", "--timeout", "5", "--", sys.executable, str(ARGV_HELPER), *args],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=15,
                check=False,
            )
            if completed.returncode != 0 or completed.stderr:
                raise VerificationFailure(
                    "agent-exec return=%d stderr=%r"
                    % (completed.returncode, completed.stderr)
                )
            harness_observed = parse_single_json(completed.stdout)
        except (subprocess.TimeoutExpired, VerificationFailure, json.JSONDecodeError) as exc:
            counts["ORACLE_INCONCLUSIVE"] += 1
            if len(mismatches) < 3:
                mismatches.append({"case": index, "reason": str(exc)})
            continue
        if core_observed == harness_observed:
            counts["MATCH"] += 1
        else:
            counts["SEMANTIC_DIFFERENCE"] += 1
            mismatches.append(
                {"case": index, "input": args, "coreguard": core_observed, "agent_exec": harness_observed}
            )
    if counts["SEMANTIC_DIFFERENCE"]:
        raise VerificationFailure(
            "differential mismatch requires isolation: %s" % mismatches[0]
        )
    return {
        "status": "PASS" if not counts["ORACLE_INCONCLUSIVE"] else "BLOCKED",
        "cases": len(cases),
        "classification": dict(counts),
        "seed": seed ^ 0xA5A5,
        "mismatches_or_inconclusive_examples": mismatches,
        "oracle": str(agent_exec),
    }


def run_full(
    exe: pathlib.Path,
    agent_exec: pathlib.Path | None,
    seed: int,
    boundary_timeout_ms: int,
) -> dict[str, Any]:
    sections = {
        "json_contract": run_json_contract(exe),
        "process_metrics": run_metrics_contract(exe),
        "job_metrics": run_job_metrics_contract(exe),
        "resource_enforcement": run_resource_enforcement(exe),
        "cpu_enforcement": run_cpu_enforcement(exe),
        "active_process_enforcement": run_active_process_enforcement(exe),
        "argument_roundtrip_and_properties": run_roundtrip(exe, seed),
        "timeout_boundary": run_timeout_boundary(exe, boundary_timeout_ms),
        "failure_injection": run_failure_injection(exe),
        "handle_lifecycle_stress": run_handle_stress(exe),
        "differential_testing": run_differential(exe, agent_exec, seed),
    }
    blocked = [
        name for name, section in sections.items() if section.get("status") == "BLOCKED"
    ]
    return {
        "status": "PASS" if not blocked else "PASS_WITH_BLOCKED_ORACLE",
        "seed": seed,
        "sections": sections,
        "blocked_sections": blocked,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    parser.add_argument(
        "--agent-exec",
        type=pathlib.Path,
        help="Optional external executable used for differential comparison",
    )
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=SEED)
    parser.add_argument("--boundary-timeout-ms", type=int, default=500)
    args = parser.parse_args()
    try:
        require_windows()
        if not args.exe.is_file():
            raise VerificationFailure("coreguard executable not found: %s" % args.exe)
        report = run_full(
            args.exe.resolve(),
            args.agent_exec.resolve() if args.agent_exec else None,
            args.seed,
            args.boundary_timeout_ms,
        )
    except VerificationFailure as exc:
        report = {"status": "FAIL", "error": str(exc), "seed": args.seed}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("PASS", "PASS_WITH_BLOCKED_ORACLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
