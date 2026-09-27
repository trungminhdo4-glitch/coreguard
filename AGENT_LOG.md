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

### 2026-09-25 14:15 - Verification gate proves CPU-limit enforcement latency

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | `tests/verification.py` rejects CPU-limit runs that are only classified after natural exit or after the kernel's late end-of-job backstop |
| Commit | `dc894be` |
| Ergebnis | OK - pre-enforcement binary 479dbb2 now FAILs the gate (200 ms limit: 3.1-7.0 s wall, 2.6-5.0 s job CPU); fixed binary PASSes (40 samples: 214-368 ms wall, 203 ms job CPU); 122 unittest OK (2 skipped); full gate PASS, `blocked_sections: []` |

Details:

- Gap: `run_cpu_enforcement` and `run_job_metrics_contract` asserted classification (`returncode 123`, `resource_limit`, kind `cpu_time`, `cleanup_ok`) but no timing. On 479dbb2 the enforcing accounting poll does not exist, so a 200 ms limit was reported only after the workload finished (root) or after the kernel's periodic backstop (tree/job-metrics, ~5-7 s) while the gate still passed. Microsoft documents the job-time backstop only as a periodic check, so it must not be treated as the enforcement mechanism.
- Change: helper `assert_prompt_cpu_enforcement(label, payload)` used by the root case, both process-tree cases and the job-metrics CPU experiment; it requires `duration_ms <= CPU_ENFORCEMENT_PROMPT_BOUND_MS` (3000, matching the shipped `test_coreguard.py` bound) and `job_metrics.total_user_cpu_ms <= CPU_ENFORCEMENT_LIMIT_MS * CPU_ENFORCEMENT_MAX_JOB_CPU_FACTOR` (8x the limit). Test-only; no product, CLI or ABI change. The CPU sections report duration and job CPU as evidence.
- Negative controls: 479dbb2 fails with the new messages; disabling the wall bound isolates the job-CPU check (5000 ms job CPU), disabling the job-CPU check isolates the wall check (tree 5952 ms; the root run slipped below 3000 ms in that sample, which is why the accounting invariant matters); synthetic payloads exercise the helper in both directions.
- Positive control: fixed binary 10/10 `run_cpu_enforcement` sections PASS; full gate PASS; 122 unittest cases OK (2 skipped) with `COREGUARD_VCVARS` set.
- Deliberately unchanged: the natural-exit boundary loop (10 runs, `--burn 0.15` against a 200 ms limit) accepts both `exited` and `resource_limit`; it is a boundary fixture, not an enforcement proof, so no bound was added there.
- Not verified: GitHub Actions (local MSVC substitute). Push/PR only on the dedicated feature branch.

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
