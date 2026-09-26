# Agent Log

### 2026-09-23 00:20 - CPU-only wait polling removed

| Feld | Wert |
|---|---|
| Agent | Codex |
| Task | CPU-time-only runs block on process/job signals instead of waking every 5 ms |
| Commit | - |
| Ergebnis | OK - MSVC /W4 /WX /analyze; 53 tests and 28 subtests passed |

### 2026-09-24 - Execution context: cg_run_ex, CLI --cwd/--env/--capture-limit-bytes

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | Additive execution-context API (`cg_exec_context` + `cg_run_ex`/`cg_run_ex_with_job_metrics`), CLI parity, fail-closed validation, bounded per-run capture prefix |
| Commit | `242fd6d` |
| Ergebnis | OK - MSVC Release /W4 /WX /analyze clean; 122 unittest cases OK (2 skipped); `tests/verification.py` PASS (12 sections, 0 blocked, execution_context 4 cases) |

Details:

- Branch `agent/coreguard-execution-context` on `origin/main` (`29cf2f5`), includes cherry-picks `bdfe23d` (cleanup_ok final-state fix from `agent/coreguard-exited-cleanup-truth`) and `2baa66d` (Codex CPU-only wait perf, previously local-only).
- `cg_run_options` (40 B) unchanged; `cg_exec_context` (32 B) + two entry points added. `NULL` context equals the old behavior; invalid values (empty cwd, block not exactly double-NUL terminated, size without block, capture prefix > 1 GiB) return `USAGE_ERROR` before spawn.
- Capture bound moved from `CG_OUTPUT_LIMIT` to `cg_capture_pipe.limit` (default 1 MiB, max 1 GiB); the source-order invariant in `tests/test_bounded_output_source.py` now pins `capture->limit = limit` and the clamped read.
- CLI `--env` merges case-insensitively (`CompareStringOrdinal`) over the caller environment and sorts the block (Win32 convention); duplicate or malformed entries exit 2. `--capture-limit-bytes` requires `--json`.
- Evidence: fresh CMake Release build; `python -m unittest discover -s tests -p "test_*.py"`; `python tests/verification.py --exe build\Release\coreguard.exe`; new consumer probe `tests/consumers/exec_context/main.c` (null context, cwd, empty/malformed environment, capture bounds).
- Not verified: GitHub Actions (local MSVC substitute), Linux/macOS (Windows-only project), Netcup remote. Not pushed (owner gate).

### 2026-09-24 - Flake fix: CPU-limit margin

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | `test_cpu_limit_is_not_wall_clock_timeout` was environment-flaky at a 50 ms job CPU limit |
| Commit | `b47c144` |
| Ergebnis | OK - limit raised to 500 ms; classification intent unchanged |

Details:

- Measured the sleeping Python child's user CPU on the unchanged published line: 0-62 ms across 16 runs, 1 run tripped the 50 ms limit (identical failure mode on `origin/main`, so not a regression of this branch).
- The child still sleeps ~1 s against a 1500 ms wall timeout; the test keeps proving that a CPU limit is not reported as a wall-clock timeout.

### 2026-09-26 11:00 - Containment evidence without PID publication

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | Tree-timeout containment is verified through a named-object survivor oracle, so an unpublished-PID leak can no longer hide behind the retry |
| Commit | `6988a73` |
| Ergebnis | OK - pristine gate PASSes with a live unpublished survivor (A/B), changed gate FAILs (same survivor), passes after cleanup; 125 unittest OK (2 skipped); full gate PASS, `blocked_sections: []` |

Details:

- Gap: `run_tree_timeout` could only check PIDs the fixture wrote to disk; when the kill fired before publication it retried with the same pid_file and checked only the retry's PIDs, so a first-attempt survivor was unobservable.
- Oracle: the fixture creates a session-unique `Local\CoreGuard-<uuid>` mutex before publishing PIDs and holds it for process lifetime. `OpenMutexW(SYNCHRONIZE)` success proves a holder; `ERROR_FILE_NOT_FOUND` is the only error treated as clean; every probe handle is closed immediately. Bounded enumeration by unique argv token was prototyped and rejected as the shipped oracle: 149 of 283 processes were unreadable in a full sweep (access denied / invalid parameter), so a missing token proves nothing, and `NtQueryInformationProcess(ProcessCommandLineInformation=60)` is undocumented. Marker: two syscalls, no enumeration, deterministic positive.
- Wiring: `run_tree_timeout` uses one marker per call for both attempts and checks absence after the retry, so the retry can no longer mask a first-attempt leak; the section reports `survivor_marker_cleared`.
- Controls: injected unpublished survivor -> old gate PASS / new gate FAIL / new gate PASS after kill; 10/10 `run_tree_timeout` runs stable; direct oracle lifetime check (absent -> create -> holder -> close -> absent); legacy fixture shapes (`pid_file` and `--grandchild`) publish PIDs unchanged.
- Bound and wording: presence proves a survivor; absence is a bounded clearance signal for cooperative fixture processes whose first action is the marker call. No README/SECURITY change, no false security promise.
- Not verified: GitHub Actions (local MSVC substitute); no product code touched, binary unchanged (0ffd8aa build).
