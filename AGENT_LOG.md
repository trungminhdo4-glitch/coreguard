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

### 2026-09-26 - Handle-stress predicate fails only on sustained growth

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | `run_handle_stress` counted isolated one-time handle shifts as leakage; a correct cold verifier process failed with two shifts |
| Commit | `562ee16` |
| Ergebnis | OK - old predicate FAILs the reproduced cold run (`[133,139x6,146,146,146]`, matches the recorded wave257 cold failure `[132,138x6,145x3]`), new predicate PASSes it; injected 1-handle-per-stress-case leak, late-start leak and calibration-only leak still FAIL; 10/10 cold runs PASS, 3/3 leak injections FAIL; 122 unittest OK (2 skipped); full gate PASS |

Details:

- Defect: the parent verifier's handle count takes isolated one-time batch shifts that are unrelated to accumulation - measured `+6` at the first subprocess spawn and `+7` at the first tree timeout, both flat afterwards. `len(positive_jumps) > 1` failed such a correct run; the identical code passed when earlier verification sections had already warmed the process, so the verdict depended on process history, not on the stress loop. Recorded earlier in `reports/wave257/HANDOFF.md` and `reports/wave260/HANDOFF.md` as an open, separate defect.
- Change: `growth_windows` records every sampling window whose delta exceeds `HANDLE_COUNT_TOLERANCE`; failure now requires growth in two consecutive windows (or in the calibration call, unchanged). Isolated one-time shifts are tolerated and reported. Test-only; no product, CLI or ABI change.
- Falsifiers (real `CreateEventW` handles injected through the production `stress_case` path): 1 handle per case -> FAIL `grew across consecutive windows`; leak starting at case 40 -> FAIL; +6 in the calibration call only -> FAIL `continued growing`; 10-handle transient opened at case 9 and closed at case 20 -> PASS. Known residual: two benign >4 shifts in adjacent windows would still fail, and the stricter sensitivity of the old predicate for probabilistic per-call leaks (p=0.5: 79% -> 50% detection; uniform per-call leak stays 100%) is the price for removing the cold false positive; a one-shot permanent plateau is outside this oracle's resolution by construction.
- Positive: 10/10 cold `run_handle_stress` PASS with a deterministic `[132,135x6,142x3]` series; full `tests/verification.py` PASS (`blocked_sections: []`, samples `[142x7,143x3]`); `python -m unittest discover -s tests -p "test_*.py"` 122 OK (2 skipped); MSVC x64 Release `/W4 /WX /analyze` build clean.
- Not verified: GitHub Actions (local MSVC substitute). Push/PR only on the dedicated feature branch, owner gate.

### 2026-09-26 - Handle-stress oracle measures post-warmup net drift (supersedes the adjacency predicate)

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | Das Wave-262-Adjazenz-Praedikat tolerierte einen permanenten, ueber nicht-adjazente Fenster verteilten +15-Handle-Leak. Die dafuer verantwortlichen zwei kalten Lazy-Shifts werden jetzt per explizitem Warmup aus dem Messfenster entfernt; danach gilt eine Netto-Drift-Invariante. |
| Commit | `1830659` |
| Ergebnis | OK - Alt-Praedikat kalt reproduziert (`[132,138x6,145x3]`, == wave257-Serie); Warmup entfernt beide Shifts (net 0 in 9/9 Vorlaeufen, kalt 10/10); alle Pflicht-Falsifier FAILen inkl. spaced +15; Transient PASS; 122 unittest OK (2 skipped); volles Gate PASS; MSVC /W4 /WX /analyze clean |

Details:

- Attributionsmessung (frischer Prozess, MEASURED): erster `subprocess.run` +3 Handles; erstes `TemporaryDirectory` create+delete +7 (nicht der Kill, nicht rmtree allein, nicht OpenProcess); danach jede weitere Operation 0. Beide Shifts sind synchron und einmalig.
- Warmup-Varianten (je frischer Prozess, 56-Fall-Sequenz, Netto final-baseline): ohne Warmup +10; nur `normal` +7; `normal` + `tree_timeout` net 0 in 6/6 (plus `all`-Warmup 3/3 net 0). Deshalb Warmup exakt aus den zwei Shift-Ausloesern; beide laufen den normalen Produktpfad mit ihren Cleanup-Assertions.
- Neue Invariante: `HANDLE_DRIFT_TOLERANCE = 2`, FAIL nur bei `final - baseline > 2`; der Kalibrierungs-Call entfaellt (unter Netto-Check redundant). Begruendung der 2: gemessener benigner Netto-Drift 0 in 10/10 kalt und 6/6 warm, +1 im warmen Gate; kleinster geforderter Falsifier +5 -> 1 Handle Kopfraum, 3 Abstand.
- Falsifier mit echten `CreateEventW`-Handles durch den Produktionspfad: uniform +1/Kall net 56 FAIL; +5 bei Calls 1/17/33 net 15 FAIL (das fruehere Hole); einmaliges +5 Plateau net 5 FAIL; +1 alle 5 Calls net 11 FAIL; 10-Handle-Transient geschlossen net 0 PASS; kein Injekt net 0 PASS 10/10; warm (2 Laeufe im selben Prozess) 6/6 net 0.
- Loop A: `cg-oldgate` (unveraendertes main a9fbf53) FAILt kalt mit `[132,138x6,145x3]`; das neue Orakel besteht dieselben Konstellationen.
- Adversarial (read-only Subagent): Aufloesungsgrenzen praezise dokumentiert - <=2 permanente Handles unsichtbar (Toleranzentscheidung), pre-baseline-Masking nur mit injiziertem Verifier-Zustand (produktpfad-unerreichbar), gc-collectible Akkumulation per Design unsichtbar, Warmup-Erstaufrufe sind vom Vertrag ausgenommen; `GetProcessHandleCount`-Fehler fail-closed.
- Regression: `python -m unittest discover -s tests -p "test_*.py"` 122 OK (2 skipped); `tests/verification.py` PASS (`blocked_sections: []`, 32,4 s; Handle-Section 4,9 s, 58 Faelle = 2 Warmup + 56); MSVC x64 Release `/W4 /WX /analyze` clean. Kein CI-Kostenanstieg gegenueber 389b380 (Gate 32,3 s). Test-only, keine Produkt-/CLI-/ABI-/Packaging-Aenderung.
- Ersetzt das Adjazenz-Praedikat aus `562ee16`/`389b380`; Branch `agent/coreguard-handle-invariant` bleibt lokal und unpubliziert. Wave 261b (`d3249bc`) ist remote, aber noch ohne PR/CI; Publikation bleibt Owner-Gate.
