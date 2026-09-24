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

### 2026-09-24 - Bounded stdin payload + CPU-limit enforcement fix

| Feld | Wert |
|---|---|
| Agent | OpenCode |
| Task | `cg_exec_context.stdin_data`/`stdin_size` (max. 64 KiB, thread-freie Pipe), CLI `--stdin-file`, plus Regressions-Fix fuer die CPU-Limit-Durchsetzung |
| Commit | `1ff3a56` (stdin), `396a898` (CPU-Fix) |
| Ergebnis | OK - Clean-Rebuild MSVC Release /W4 /WX /analyze; 126 unittest OK (2 skip); verification.py PASS (execution_context 6 Cases, cpu_enforcement PASS) |

Details:

- Branch `agent/coreguard-stdin-context`, gestapelt auf `agent/coreguard-execution-context` (PR #11).
- stdin: 128-KiB-Pipe-Puffer, Payload (1 B..64 KiB) wird vor `ResumeThread` geschrieben und die Write-Seite geschlossen. Ein WriteFile bis zur Puffergroesse blockiert nie (empirisch belegt: 64 KiB in 64-KiB-Puffer ok, 128 KiB in 64-KiB-Puffer blockiert) -> kein Writer-Thread, kein Deadlock bei einem Kind, das stdin ignoriert. `NULL` erbt weiterhin das Eltern-stdin.
- CLI `--stdin-file PATH` (rohe Bytes; fehlende/leere/zu grosse Dateien -> Exit 2). Layout-Manifest: 48 B, Offsets stdin_data=32, stdin_size=40.
- **Befund + Fix**: Der Commit `2baa66d` (Codex, in PR #11) setzt CPU-only-Limits nicht mehr durch: Ein Job-Handle signalisiert beim Prozess-Exit, nicht beim Erreichen des CPU-Limits. Gemessen mit demselben 5-s-Burner gegen `origin/main` (29cf2f5): 200 ms Limit -> ~0.24 s vs. 1.9-5.1 s; 2000 ms Limit -> ~2.0 s vs. ~5.1 s. `396a898` stellt den 5-ms-Accounting-Poll wieder her und entfernt den toten Job-Wait-Zweig; danach wieder ~0.21-0.26 s bzw. ~2.05-2.13 s (gleich zur Baseline). Der Enforcement-Test hat jetzt eine Laufzeitschranke.
- Gotcha: Ein inkrementeller MSBuild-Rebuild lieferte ein stale Binary (der Fix schien zunaechst wirkungslos); erst ein Clean-Rebuild zeigte das korrekte Verhalten. Bei Enforcement-Messungen `build/` vorher loeschen.
- Beweise: `python -m unittest discover -s tests -p "test_*.py"` (126 OK, 2 skip); `python tests/verification.py --exe build\Release\coreguard.exe` PASS; Messreihen `cg_burn_probe`/`cg_cpu_ticks_probe` gegen den `origin/main`-Build.
- Nicht gepusht (Owner-Gate). Wichtig fuer die Integration: PR #11 enthaelt den Regressions-Commit; dieser Branch muss mit oder direkt nach #11 gemergt werden.
