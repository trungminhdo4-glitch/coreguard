"""Bounded active-process Job Object fixtures for coreguard tests."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time


SCRIPT = pathlib.Path(__file__).resolve()


def write_report(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def attempt_child(duration: float) -> tuple[dict[str, object], subprocess.Popen[bytes] | None]:
    try:
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--sleep", str(duration)],
            close_fds=True,
        )
    except OSError as exc:
        return (
            {
                "ok": False,
                "errno": exc.errno,
                "winerror": getattr(exc, "winerror", None),
                "error": str(exc),
            },
            None,
        )
    return ({"ok": True, "pid": child.pid}, child)


def run_helper(mode: str, report: pathlib.Path, count: int = 0) -> int:
    if mode == "attempt-child":
        result, _ = attempt_child(0.75)
        write_report(report, {"attempts": [result], "root_exit_code": 0})
        time.sleep(0.5)
        return 0

    if mode == "parallel":
        attempts = []
        for _ in range(count):
            result, _ = attempt_child(0.75)
            attempts.append(result)
        write_report(report, {"attempts": attempts, "root_exit_code": 0})
        time.sleep(0.5)
        return 0

    if mode == "sequential":
        attempts = []
        for _ in range(2):
            result, child = attempt_child(0.15)
            attempts.append(result)
            if child is not None:
                child.wait(timeout=3)
        write_report(report, {"attempts": attempts, "root_exit_code": 0})
        return 0

    if mode == "nested-child":
        result, _ = attempt_child(0.75)
        write_report(report, {"attempts": [result], "root_exit_code": 0})
        time.sleep(0.5)
        return 0

    if mode == "grandchild":
        nested_report = pathlib.Path(str(report) + ".nested")
        nested = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--helper", "nested-child", str(nested_report)],
            close_fds=True,
        )
        deadline = time.monotonic() + 3
        while not nested_report.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        write_report(report, {"nested_pid": nested.pid, "root_exit_code": 0})
        time.sleep(0.5)
        return 0

    if mode == "explosion":
        fanout_reports = [pathlib.Path(f"{report}.fanout-{index}") for index in range(count)]
        fanout_pids = []
        for fanout_report in fanout_reports:
            fanout_pids.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--helper",
                        "parallel",
                        str(fanout_report),
                        "3",
                    ],
                    close_fds=True,
                ).pid
            )
        deadline = time.monotonic() + 3
        while (
            not all(path.is_file() for path in fanout_reports)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        write_report(
            report,
            {"fanout_pids": fanout_pids, "fanout_reports": [str(path) for path in fanout_reports]},
        )
        time.sleep(0.5)
        return 0

    if mode == "sleep":
        time.sleep(float(count))
        return 0

    return 2


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--helper":
        mode = sys.argv[2]
        report = pathlib.Path(sys.argv[3])
        count = int(sys.argv[4]) if len(sys.argv) >= 5 else 0
        return run_helper(mode, report, count)
    if len(sys.argv) == 3 and sys.argv[1] == "--sleep":
        time.sleep(float(sys.argv[2]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
