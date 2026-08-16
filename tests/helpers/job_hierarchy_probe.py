"""Bounded Windows Job Object probes for Coreguard Wave 9.

The fixture deliberately uses ctypes instead of adding a production API.  It
keeps descendants at a kernel-event barrier so PID-list and lifecycle checks
do not depend on a sleep being long enough.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes


SCRIPT = pathlib.Path(__file__).resolve()
ERROR_MORE_DATA = 234
ERROR_SUCCESS = 0
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
STARTF_USESTDHANDLES = 0x00000100
STD_INPUT_HANDLE = -10
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_HANDLES = 0x00000001

HANDLE = wintypes.HANDLE
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("value", ctypes.c_ulonglong)] * 6


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTime", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


class JOBOBJECT_BASIC_PROCESS_ID_LIST_HEADER(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.CreateEventW.argtypes = [
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
KERNEL32.CreateEventW.restype = HANDLE
KERNEL32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD,
    wintypes.DWORD,
    HANDLE,
]
KERNEL32.CreateFileW.restype = HANDLE
KERNEL32.CreateJobObjectW.argtypes = [
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.LPCWSTR,
]
KERNEL32.CreateJobObjectW.restype = HANDLE
KERNEL32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
KERNEL32.CreateProcessW.restype = wintypes.BOOL
KERNEL32.GetCurrentProcess.argtypes = []
KERNEL32.GetCurrentProcess.restype = HANDLE
KERNEL32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(wintypes.DWORD)]
KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = HANDLE
KERNEL32.GetStdHandle.argtypes = [ctypes.c_int]
KERNEL32.GetStdHandle.restype = HANDLE
KERNEL32.IsProcessInJob.argtypes = [HANDLE, HANDLE, ctypes.POINTER(wintypes.BOOL)]
KERNEL32.IsProcessInJob.restype = wintypes.BOOL
KERNEL32.QueryInformationJobObject.argtypes = [
    HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
KERNEL32.QueryInformationJobObject.restype = wintypes.BOOL
KERNEL32.ResumeThread.argtypes = [HANDLE]
KERNEL32.ResumeThread.restype = wintypes.DWORD
KERNEL32.SetEvent.argtypes = [HANDLE]
KERNEL32.SetEvent.restype = wintypes.BOOL
KERNEL32.SetInformationJobObject.argtypes = [
    HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
KERNEL32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
KERNEL32.TerminateProcess.restype = wintypes.BOOL
KERNEL32.WaitForSingleObject.argtypes = [HANDLE, wintypes.DWORD]
KERNEL32.WaitForSingleObject.restype = wintypes.DWORD


def win32_error() -> int:
    error = ctypes.get_last_error()
    return int(error if error != ERROR_SUCCESS else 1)


def write_json(path: pathlib.Path, values: object) -> None:
    path.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")


def wait_for_json(path: pathlib.Path, timeout_seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error = "report was not published"
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            else:
                if isinstance(value, dict):
                    return value
                last_error = "report was not a JSON object"
        time.sleep(0.01)
    raise RuntimeError(last_error)


def query_job_pids(job: HANDLE | None) -> tuple[list[int], int]:
    capacity = 16
    header_size = ctypes.sizeof(JOBOBJECT_BASIC_PROCESS_ID_LIST_HEADER)
    pointer_size = ctypes.sizeof(ULONG_PTR)
    for _ in range(8):
        buffer_size = header_size + capacity * pointer_size
        buffer = ctypes.create_string_buffer(buffer_size)
        returned = wintypes.DWORD()
        success = KERNEL32.QueryInformationJobObject(
            job,
            JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.cast(buffer, wintypes.LPVOID),
            buffer_size,
            ctypes.byref(returned),
        )
        header = ctypes.cast(
            buffer, ctypes.POINTER(JOBOBJECT_BASIC_PROCESS_ID_LIST_HEADER)
        ).contents
        assigned = int(header.NumberOfAssignedProcesses)
        listed = int(header.NumberOfProcessIdsInList)
        if success and listed >= assigned:
            values = (ULONG_PTR * listed).from_buffer(
                buffer, header_size
            ) if listed else []
            return [int(value) for value in values], assigned
        if assigned > capacity:
            capacity = assigned
        elif not success and win32_error() == ERROR_MORE_DATA:
            capacity *= 2
        elif success and listed < assigned:
            capacity = max(capacity * 2, assigned)
        else:
            raise ctypes.WinError(win32_error())
    raise RuntimeError("JobObjectBasicProcessIdList did not converge")


def is_in_job(process: HANDLE, job: HANDLE | None) -> bool:
    result = wintypes.BOOL()
    if not KERNEL32.IsProcessInJob(process, job, ctypes.byref(result)):
        raise ctypes.WinError(win32_error())
    return bool(result.value)


def create_event(name: str) -> HANDLE:
    event = KERNEL32.CreateEventW(None, True, False, name)
    if not event:
        raise ctypes.WinError(win32_error())
    return event


def set_job_limits(
    job: HANDLE,
    flags: int,
    active_process_limit: int = 0,
    memory_limit_bytes: int = 0,
    cpu_time_ms: int = 0,
) -> None:
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = flags
    limits.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    limits.JobMemoryLimit = memory_limit_bytes
    limits.BasicLimitInformation.PerJobUserTimeLimit.QuadPart = cpu_time_ms * 10000
    if not KERNEL32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        raise ctypes.WinError(win32_error())


def assign_current(job: HANDLE) -> None:
    if not KERNEL32.AssignProcessToJobObject(job, KERNEL32.GetCurrentProcess()):
        raise ctypes.WinError(win32_error())


def create_child_worker(
    release_event_name: str,
    start_event_name: str,
    report: pathlib.Path,
    mode: str,
    value: int,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--worker",
            release_event_name,
            start_event_name,
            str(report),
            mode,
            str(value),
        ],
        close_fds=True,
    )


def run_grandworker(
    release_event_name: str,
    start_event_name: str,
    report: pathlib.Path,
    mode: str,
    value: int,
) -> int:
    release_event = create_event(release_event_name)
    start_event = create_event(start_event_name)
    data: bytearray | None = None
    try:
        write_json(report, {"pid": os.getpid()})
        KERNEL32.WaitForSingleObject(start_event, INFINITE)
        if mode in ("memory", "memory-catch"):
            try:
                data = bytearray(value * 1024 * 1024)
                for index in range(0, len(data), 4096):
                    data[index] = 1
            except MemoryError as exc:
                if mode == "memory-catch":
                    write_json(report, {"pid": os.getpid(), "memory_error": str(exc)})
                    KERNEL32.SetEvent(release_event)
                    return 0
                raise
        if mode in ("burn", "cpu"):
            accumulator = 0
            while KERNEL32.WaitForSingleObject(release_event, 0) == WAIT_TIMEOUT:
                accumulator = (accumulator + 1) & 0xFFFFFFFF
            if accumulator == -1:
                return 2
        else:
            KERNEL32.WaitForSingleObject(release_event, INFINITE)
    finally:
        KERNEL32.CloseHandle(start_event)
        KERNEL32.CloseHandle(release_event)
    return 0


def run_worker(
    release_event_name: str,
    start_event_name: str,
    report: pathlib.Path,
    mode: str,
    value: int,
) -> int:
    release_event = create_event(release_event_name)
    start_event = create_event(start_event_name)
    grand_report = pathlib.Path(str(report) + ".grandchild")
    grandchild: subprocess.Popen | None = None
    try:
        try:
            grandchild = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--grandworker",
                    release_event_name,
                    start_event_name,
                    str(grand_report),
                    mode,
                    str(value),
                ],
                close_fds=True,
            )
            grand_error = None
        except OSError as exc:
            grand_error = {
                "errno": exc.errno,
                "winerror": getattr(exc, "winerror", None),
                "error": str(exc),
            }
        grand_report_value = None
        if grandchild is not None:
            try:
                grand_report_value = wait_for_json(grand_report)
            except RuntimeError:
                grand_report_value = None
        write_json(
            report,
            {
                "pid": os.getpid(),
                "grandchild_pid": grandchild.pid if grandchild is not None else None,
                "grandchild_report": grand_report_value,
                "grandchild_error": grand_error,
            },
        )
        KERNEL32.WaitForSingleObject(start_event, INFINITE)
        KERNEL32.WaitForSingleObject(release_event, INFINITE)
        if grandchild is not None:
            grandchild.wait(timeout=5)
    finally:
        KERNEL32.CloseHandle(start_event)
        KERNEL32.CloseHandle(release_event)
    return 0


def run_oracle(report: pathlib.Path) -> int:
    release_event_name = f"Local\\CoreguardJobOracleRelease-{os.getpid()}"
    start_event_name = f"Local\\CoreguardJobOracleStart-{os.getpid()}"
    release_event = create_event(release_event_name)
    start_event = create_event(start_event_name)
    worker_report = pathlib.Path(str(report) + ".worker")
    worker: subprocess.Popen | None = None
    try:
        worker = create_child_worker(
            release_event_name, start_event_name, worker_report, "idle", 0
        )
        worker_value = wait_for_json(worker_report)
        pids, assigned = query_job_pids(None)
        write_json(
            report,
            {
                "root_pid": os.getpid(),
                "child_pid": worker_value["pid"],
                "grandchild_pid": worker_value["grandchild_pid"],
                "job_pids": pids,
                "assigned_processes": assigned,
                "expected_pids": [
                    os.getpid(),
                    worker_value["pid"],
                    worker_value["grandchild_pid"],
                ],
            },
        )
        KERNEL32.SetEvent(start_event)
        KERNEL32.SetEvent(release_event)
        worker.wait(timeout=5)
    finally:
        KERNEL32.CloseHandle(start_event)
        KERNEL32.CloseHandle(release_event)
    return 0


def run_nested(report: pathlib.Path, mode: str, value: int) -> int:
    release_event_name = f"Local\\CoreguardNestedJobRelease-{os.getpid()}"
    start_event_name = f"Local\\CoreguardNestedJobStart-{os.getpid()}"
    release_event = create_event(release_event_name)
    start_event = create_event(start_event_name)
    job = KERNEL32.CreateJobObjectW(None, None)
    if not job:
        write_json(report, {"setup_error": win32_error(), "stage": "CreateJobObject"})
        KERNEL32.CloseHandle(start_event)
        KERNEL32.CloseHandle(release_event)
        return 3
    worker: subprocess.Popen | None = None
    try:
        try:
            assign_current(job)
        except OSError as exc:
            write_json(
                report,
                {
                    "setup_error": getattr(exc, "winerror", None),
                    "stage": "AssignProcessToJobObject",
                },
            )
            return 3
        try:
            worker = create_child_worker(
                release_event_name,
                start_event_name,
                pathlib.Path(str(report) + ".worker"),
                mode,
                value,
            )
            worker_error = None
        except OSError as exc:
            worker_error = {
                "errno": exc.errno,
                "winerror": getattr(exc, "winerror", None),
                "error": str(exc),
            }
        worker_report = pathlib.Path(str(report) + ".worker")
        worker_value = wait_for_json(worker_report) if worker is not None else {}
        pids, assigned = query_job_pids(job)
        root_pid = os.getpid()
        observed = {
            "root_pid": root_pid,
            "child_pid": worker_value.get("pid"),
            "grandchild_pid": worker_value.get("grandchild_pid"),
            "child_job_pids": pids,
            "assigned_processes": assigned,
            "grandchild_report": worker_value.get("grandchild_report"),
            "expected_pids": [
                value for value in [root_pid, worker_value.get("pid"), worker_value.get("grandchild_pid")] if value
            ],
            "root_in_child_job": is_in_job(KERNEL32.GetCurrentProcess(), job),
            "child_in_child_job": False,
            "grandchild_in_child_job": False,
            "worker_error": worker_error,
            "mode": mode,
        }
        if worker is not None and worker_value.get("pid"):
            child_handle = KERNEL32.OpenProcess(0x1000 | 0x00100000, False, worker_value["pid"])
            if child_handle:
                try:
                    observed["child_in_child_job"] = is_in_job(child_handle, job)
                finally:
                    KERNEL32.CloseHandle(child_handle)
        if worker_value.get("grandchild_pid"):
            grand_handle = KERNEL32.OpenProcess(0x1000 | 0x00100000, False, worker_value["grandchild_pid"])
            if grand_handle:
                try:
                    observed["grandchild_in_child_job"] = is_in_job(grand_handle, job)
                finally:
                    KERNEL32.CloseHandle(grand_handle)
        write_json(report, observed)
        KERNEL32.SetEvent(start_event)
        if mode == "normal":
            KERNEL32.SetEvent(release_event)
            if worker is not None:
                worker.wait(timeout=5)
        else:
            KERNEL32.WaitForSingleObject(release_event, INFINITE)
    finally:
        if worker is not None and worker.poll() is None and mode == "normal":
            worker.terminate()
        KERNEL32.CloseHandle(start_event)
        KERNEL32.CloseHandle(release_event)
        KERNEL32.CloseHandle(job)
    return 0


def create_process(
    command: list[str],
    current_directory: pathlib.Path | None = None,
    stdout_handle: HANDLE | None = None,
    stderr_handle: HANDLE | None = None,
    flags: int = 0,
) -> PROCESS_INFORMATION:
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    if stdout_handle is not None and stderr_handle is not None:
        startup.dwFlags = STARTF_USESTDHANDLES
        startup.hStdInput = KERNEL32.GetStdHandle(STD_INPUT_HANDLE)
        startup.hStdOutput = stdout_handle
        startup.hStdError = stderr_handle
    information = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if not KERNEL32.CreateProcessW(
        None,
        command_line,
        None,
        None,
        bool(stdout_handle is not None and stderr_handle is not None),
        flags | CREATE_UNICODE_ENVIRONMENT,
        None,
        str(current_directory) if current_directory is not None else None,
        ctypes.byref(startup),
        ctypes.byref(information),
    ):
        raise ctypes.WinError(win32_error())
    return information


def run_breakaway(report: pathlib.Path) -> int:
    command = [sys.executable, str(SCRIPT), "--sleep", "30"]
    try:
        information = create_process(command, flags=CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB)
        if KERNEL32.ResumeThread(information.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(win32_error())
        in_any_job = is_in_job(information.hProcess, None)
        write_json(
            report,
            {
                "create_success": True,
                "win32_error": None,
                "child_pid": int(information.dwProcessId),
                "in_any_job": in_any_job,
            },
        )
        KERNEL32.CloseHandle(information.hThread)
        KERNEL32.CloseHandle(information.hProcess)
        return 0
    except OSError as exc:
        write_json(
            report,
            {
                "create_success": False,
                "win32_error": getattr(exc, "winerror", None),
                "child_pid": None,
                "in_any_job": None,
                "error": str(exc),
            },
        )
        return 0


def create_output_file(path: pathlib.Path) -> HANDLE:
    attributes = SECURITY_ATTRIBUTES(
        ctypes.sizeof(SECURITY_ATTRIBUTES), None, True
    )
    handle = KERNEL32.CreateFileW(
        str(path),
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        ctypes.byref(attributes),
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or int(handle) == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(win32_error())
    return handle


def close_process_information(information: PROCESS_INFORMATION) -> None:
    KERNEL32.CloseHandle(information.hThread)
    KERNEL32.CloseHandle(information.hProcess)


def run_outer(args: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coreguard", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--target-report", type=pathlib.Path, required=True)
    parser.add_argument("--outer-mode", choices=("plain", "kill", "active", "memory", "cpu", "ui", "breakaway", "silent"), required=True)
    parser.add_argument("--target-mode", choices=("nested-normal", "nested-hold", "nested-memory", "nested-memory-catch", "nested-cpu", "breakaway", "oracle"), required=True)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--inner-memory-mb", type=int)
    parser.add_argument("--inner-cpu-ms", type=int)
    parser.add_argument("--inner-max-processes", type=int)
    parser.add_argument("--outer-active-processes", type=int, default=2)
    parser.add_argument("--target-value", type=int, default=64)
    options = parser.parse_args(args)

    outer_job = KERNEL32.CreateJobObjectW(None, None)
    if not outer_job:
        write_json(options.report, {"stage": "CreateJobObject", "win32_error": win32_error()})
        return 3
    output_path = pathlib.Path(str(options.report) + ".stdout")
    error_path = pathlib.Path(str(options.report) + ".stderr")
    output_handle = create_output_file(output_path)
    error_handle = create_output_file(error_path)
    information: PROCESS_INFORMATION | None = None
    outer_closed = False
    try:
        flags = 0
        active = 0
        memory = 0
        cpu = 0
        if options.outer_mode == "kill":
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        elif options.outer_mode == "active":
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            active = options.outer_active_processes
        elif options.outer_mode == "memory":
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            memory = 64 * 1024 * 1024
        elif options.outer_mode == "cpu":
            flags |= JOB_OBJECT_LIMIT_JOB_TIME
            cpu = 250
        elif options.outer_mode == "breakaway":
            flags |= JOB_OBJECT_LIMIT_BREAKAWAY_OK
        elif options.outer_mode == "silent":
            flags |= JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        set_job_limits(outer_job, flags, active, memory, cpu)
        if options.outer_mode == "ui":
            ui = JOBOBJECT_BASIC_UI_RESTRICTIONS()
            ui.UIRestrictionsClass = JOB_OBJECT_UILIMIT_HANDLES
            if not KERNEL32.SetInformationJobObject(
                outer_job,
                JOB_OBJECT_BASIC_UI_RESTRICTIONS,
                ctypes.byref(ui),
                ctypes.sizeof(ui),
            ):
                raise ctypes.WinError(win32_error())

        target_command = [
            sys.executable,
            str(SCRIPT),
            "--" + options.target_mode,
            str(options.target_report),
        ]
        if options.target_mode in ("nested-memory", "nested-memory-catch"):
            target_command.append(str(options.target_value))
        elif options.target_mode == "nested-cpu":
            target_command.append(str(options.target_value))
        inner_args = [
            str(options.coreguard),
            "run",
            "--json",
            "--timeout-ms",
            str(options.timeout_ms),
        ]
        if options.inner_memory_mb is not None:
            inner_args.extend(["--memory-limit-mb", str(options.inner_memory_mb)])
        if options.inner_cpu_ms is not None:
            inner_args.extend(["--cpu-time-limit-ms", str(options.inner_cpu_ms)])
        if options.inner_max_processes is not None:
            inner_args.extend(["--max-processes", str(options.inner_max_processes)])
        inner_args.extend(["--", *target_command])
        information = create_process(
            inner_args,
            current_directory=options.coreguard.parent.parent,
            stdout_handle=output_handle,
            stderr_handle=error_handle,
            flags=CREATE_SUSPENDED,
        )
        if not KERNEL32.AssignProcessToJobObject(outer_job, information.hProcess):
            assignment_error = win32_error()
            KERNEL32.TerminateProcess(information.hProcess, 125)
            KERNEL32.WaitForSingleObject(information.hProcess, 5000)
            write_json(options.report, {"stage": "outer_assignment", "win32_error": assignment_error})
            return 0
        if KERNEL32.ResumeThread(information.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(win32_error())
        target_value = None
        try:
            target_value = wait_for_json(options.target_report, 10.0)
        except RuntimeError:
            pass
        outer_pids, outer_assigned = query_job_pids(outer_job)
        if options.outer_mode == "kill":
            KERNEL32.CloseHandle(outer_job)
            outer_closed = True
        wait_result = KERNEL32.WaitForSingleObject(information.hProcess, 10000)
        exit_code = wintypes.DWORD()
        KERNEL32.GetExitCodeProcess(information.hProcess, ctypes.byref(exit_code))
        output = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
        payload = None
        if output.strip():
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = {"raw": output}
        write_json(
            options.report,
            {
                "outer_mode": options.outer_mode,
                "target_mode": options.target_mode,
                "coreguard_pid": int(information.dwProcessId),
                "coreguard_wait_result": int(wait_result),
                "coreguard_exit_code": int(exit_code.value),
                "coreguard_payload": payload,
                "target_report": target_value,
                "outer_job_pids": outer_pids,
                "outer_assigned_processes": outer_assigned,
                "outer_closed": outer_closed,
            },
        )
    finally:
        if information is not None:
            close_process_information(information)
        KERNEL32.CloseHandle(output_handle)
        KERNEL32.CloseHandle(error_handle)
        if not outer_closed:
            KERNEL32.CloseHandle(outer_job)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    mode = sys.argv[1]
    try:
        if mode == "--breakaway" and len(sys.argv) == 3:
            return run_breakaway(pathlib.Path(sys.argv[2]))
        if mode == "--oracle" and len(sys.argv) == 3:
            return run_oracle(pathlib.Path(sys.argv[2]))
        if mode in ("--nested-normal", "--nested-hold", "--nested-memory", "--nested-memory-catch", "--nested-cpu"):
            if len(sys.argv) not in (3, 4):
                return 2
            value = int(sys.argv[3]) if len(sys.argv) == 4 else 0
            nested_mode = mode.removeprefix("--nested-")
            return run_nested(pathlib.Path(sys.argv[2]), nested_mode, value)
        if mode == "--worker" and len(sys.argv) == 7:
            return run_worker(
                sys.argv[2],
                sys.argv[3],
                pathlib.Path(sys.argv[4]),
                sys.argv[5],
                int(sys.argv[6]),
            )
        if mode == "--grandworker" and len(sys.argv) == 7:
            return run_grandworker(
                sys.argv[2],
                sys.argv[3],
                pathlib.Path(sys.argv[4]),
                sys.argv[5],
                int(sys.argv[6]),
            )
        if mode == "--sleep" and len(sys.argv) == 3:
            time.sleep(float(sys.argv[2]))
            return 0
        if mode == "--outer":
            return run_outer(sys.argv[2:])
    except (OSError, RuntimeError, ValueError) as exc:
        if mode in ("--breakaway", "--oracle") or mode.startswith("--nested-"):
            if len(sys.argv) >= 3:
                write_json(pathlib.Path(sys.argv[2]), {"fixture_error": str(exc)})
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
