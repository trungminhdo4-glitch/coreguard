"""Small local baseline: direct Python subprocess versus coreguard CLI."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import subprocess
import sys
import time


def median_ms(samples: list[float]) -> float:
    return statistics.median(samples)


def percentile_ms(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=pathlib.Path, required=True)
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()
    command = [sys.executable, "-c", "pass"]
    direct = []
    coreguard_no_metrics = []
    coreguard = []
    coreguard_memory = []
    coreguard_cpu = []
    coreguard_active = []
    coreguard_cpu_active = []
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [str(args.exe), "run", "--timeout-ms", "5000", "--", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("coreguard no-metrics baseline run failed")
        coreguard_no_metrics.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        subprocess.run(command, capture_output=True, check=True)
        direct.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [str(args.exe), "run", "--json", "--timeout-ms", "5000", "--", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload["status"] != "exited" or payload["exit_code"] != 0:
            raise RuntimeError("coreguard baseline run failed: %r" % payload)
        coreguard.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [
                str(args.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--memory-limit-mb",
                "128",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload["status"] != "exited" or completed.returncode != 0:
            raise RuntimeError("coreguard memory-limit baseline run failed: %r" % payload)
        coreguard_memory.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [
                str(args.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--cpu-time-limit-ms",
                "5000",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload["status"] != "exited" or completed.returncode != 0:
            raise RuntimeError("coreguard CPU-limit baseline run failed: %r" % payload)
        coreguard_cpu.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [
                str(args.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--max-processes",
                "4",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload["status"] != "exited" or completed.returncode != 0:
            raise RuntimeError("coreguard active-process baseline run failed: %r" % payload)
        coreguard_active.append((time.perf_counter() - started) * 1000)
    for _ in range(args.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [
                str(args.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--cpu-time-limit-ms",
                "5000",
                "--max-processes",
                "4",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload["status"] != "exited" or completed.returncode != 0:
            raise RuntimeError("coreguard CPU-plus-active baseline run failed: %r" % payload)
        coreguard_cpu_active.append((time.perf_counter() - started) * 1000)
    direct_median = median_ms(direct)
    coreguard_no_metrics_median = median_ms(coreguard_no_metrics)
    coreguard_median = median_ms(coreguard)
    print(json.dumps({
        "iterations": args.iterations,
        "direct_subprocess": {
            "median_ms": round(direct_median, 3),
            "min_ms": round(min(direct), 3),
            "max_ms": round(max(direct), 3),
        },
        "coreguard": {
            "median_ms": round(coreguard_median, 3),
            "p95_ms": round(percentile_ms(coreguard, 0.95), 3),
            "min_ms": round(min(coreguard), 3),
            "max_ms": round(max(coreguard), 3),
        },
        "coreguard_no_metrics": {
            "median_ms": round(coreguard_no_metrics_median, 3),
            "p95_ms": round(percentile_ms(coreguard_no_metrics, 0.95), 3),
            "min_ms": round(min(coreguard_no_metrics), 3),
            "max_ms": round(max(coreguard_no_metrics), 3),
        },
        "coreguard_memory_limit_enabled": {
            "limit_mb": 128,
            "median_ms": round(statistics.median(coreguard_memory), 3),
            "p95_ms": round(percentile_ms(coreguard_memory, 0.95), 3),
            "min_ms": round(min(coreguard_memory), 3),
            "max_ms": round(max(coreguard_memory), 3),
        },
        "coreguard_cpu_time_limit_enabled": {
            "limit_ms": 5000,
            "median_ms": round(statistics.median(coreguard_cpu), 3),
            "p95_ms": round(percentile_ms(coreguard_cpu, 0.95), 3),
            "min_ms": round(min(coreguard_cpu), 3),
            "max_ms": round(max(coreguard_cpu), 3),
        },
        "coreguard_active_process_limit_enabled": {
            "max_processes": 4,
            "median_ms": round(statistics.median(coreguard_active), 3),
            "p95_ms": round(percentile_ms(coreguard_active, 0.95), 3),
            "min_ms": round(min(coreguard_active), 3),
            "max_ms": round(max(coreguard_active), 3),
        },
        "coreguard_cpu_plus_active_enabled": {
            "cpu_limit_ms": 5000,
            "max_processes": 4,
            "median_ms": round(statistics.median(coreguard_cpu_active), 3),
            "p95_ms": round(percentile_ms(coreguard_cpu_active, 0.95), 3),
            "min_ms": round(min(coreguard_cpu_active), 3),
            "max_ms": round(max(coreguard_cpu_active), 3),
        },
        "median_overhead_ms": round(coreguard_median - direct_median, 3),
        "median_overhead_percent": round((coreguard_median / direct_median - 1) * 100, 2),
        "job_metrics_median_delta_ms": round(
            coreguard_median - coreguard_no_metrics_median, 3
        ),
        "job_metrics_p95_delta_ms": round(
            percentile_ms(coreguard, 0.95)
            - percentile_ms(coreguard_no_metrics, 0.95),
            3,
        ),
        "memory_limit_median_delta_ms": round(
            statistics.median(coreguard_memory) - coreguard_median, 3
        ),
        "memory_limit_p95_delta_ms": round(
            percentile_ms(coreguard_memory, 0.95)
            - percentile_ms(coreguard, 0.95),
            3,
        ),
        "cpu_time_limit_median_delta_ms": round(
            statistics.median(coreguard_cpu) - coreguard_median, 3,
        ),
        "cpu_time_limit_p95_delta_ms": round(
            percentile_ms(coreguard_cpu, 0.95)
            - percentile_ms(coreguard, 0.95),
            3,
        ),
        "active_process_limit_median_delta_ms": round(
            statistics.median(coreguard_active) - coreguard_median, 3
        ),
        "active_process_limit_p95_delta_ms": round(
            percentile_ms(coreguard_active, 0.95)
            - percentile_ms(coreguard, 0.95),
            3,
        ),
        "cpu_plus_active_median_delta_vs_cpu_ms": round(
            statistics.median(coreguard_cpu_active)
            - statistics.median(coreguard_cpu),
            3,
        ),
        "cpu_plus_active_p95_delta_vs_cpu_ms": round(
            percentile_ms(coreguard_cpu_active, 0.95)
            - percentile_ms(coreguard_cpu, 0.95),
            3,
        ),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
