# Coreguard

[![Windows CI](https://github.com/trungminhdo4-glitch/coreguard/actions/workflows/windows-ci.yml/badge.svg)](https://github.com/trungminhdo4-glitch/coreguard/actions/workflows/windows-ci.yml)

Coreguard is a small Windows-native C execution layer. It starts one
executable directly, places it in a Windows Job Object before resuming it, and
reports bounded execution results as a C API or as a command-line JSON result.
It does not replace an application harness, scheduler, sandbox, or policy
system.

The supported v0.1.0 contract is Windows x64, MSVC, a Release build, and
static linking. No third-party runtime dependency is required beyond the
Windows SDK and the MSVC/UCRT environment.

## Build

Requirements:

- Windows x64
- CMake 3.20 or newer
- MSVC with `vcvars64.bat`

Configure and build a Release tree:

```powershell
cmake -S . -B <build-dir>
cmake --build <build-dir> --config Release
```

The portable `build-msvc.bat` helper discovers `vcvars64.bat` through `PATH`.
Set `COREGUARD_VCVARS` when the MSVC environment script is not discoverable.
It builds with `/W4 /WX /analyze` and limits the documented SDK diagnostic
suppression to the external declaration warning emitted by the current SDK.

## Install and CMake package

Install a Release build into a relocatable prefix:

```powershell
cmake --install <build-dir> --config Release --prefix <install-prefix>
```

The install tree contains:

```text
<install-prefix>/
  bin/coreguard.exe
  include/coreguard.h
  lib/coreguard.lib
  lib/cmake/coreguard/coreguardConfig.cmake
  lib/cmake/coreguard/coreguardTargets.cmake
  lib/cmake/coreguard/coreguardTargets-release.cmake
  LICENSE
```

For a versioned build, configure with `-DCOREGUARD_VERSION=0.1.0`; the exact
version package file is then installed and CPack produces
`coreguard-0.1.0-windows-x64-msvc.zip`.

An external CMake consumer can link the installed static library with:

```cmake
find_package(coreguard 0.1.0 EXACT CONFIG REQUIRED)
target_link_libraries(my_app PRIVATE coreguard::coreguard)
```

## C API

The public header is self-contained for C and C++, and C++ declarations use
`extern "C"`. A minimal C call looks like this:

```c
#include "coreguard.h"

int main(void)
{
    const wchar_t *argv[] = {L"cmd.exe", L"/c", L"exit", L"0"};
    cg_run_options options = {0};
    cg_run_result result = {0};

    options.argv = argv;
    options.argc = sizeof(argv) / sizeof(argv[0]);
    options.timeout_ms = 5000;

    if (cg_run(&options, &result) != 0) {
        cg_run_result_free(&result);
        return 1;
    }
    int exit_code = result.has_exit_code ? result.exit_code : 1;
    cg_run_result_free(&result);
    return exit_code;
}
```

`stdout_utf8` and `stderr_utf8` are library-owned buffers. Release them with
`cg_run_result_free`; do not free them with a consumer CRT. The static library
must be linked with a matching MSVC runtime configuration. There is no DLL or
import-library contract.

Captured stdout and stderr are bounded independently. Coreguard retains the
first 1 MiB of raw bytes from each stream while continuously draining and
discarding excess bytes; `output_truncated` is set when either stream exceeds
that limit. CR and CRLF newline normalization is applied after the raw-byte
limit, so a returned stream can be smaller than its retained raw prefix.

After the controlled job exits, capture readers get a short drain period.
Coreguard then repeatedly signals them to stop, cancels synchronous reads, and
polls for confirmed termination for up to five seconds per reader. If Windows
still cannot confirm termination, the reader's buffers and handles are rarely
quarantined instead of being freed unsafely. Reaping checks a fixed number of
quarantined readers per invocation. Sixteen reader slots are shared by active
and quarantined captures; if no two slots are available, a new captured run
fails with an internal `ERROR_NOT_ENOUGH_QUOTA` result before its process is
created. Runs without output capture do not consume these slots.

## CLI

Coreguard starts the command after it has established the Job Object boundary;
it does not insert `cmd.exe` between Coreguard and the requested executable.

```text
coreguard run [--json] [--timeout-ms N]
              [--memory-limit-mb N]
              [--cpu-time-limit-ms N]
              [--max-processes N] -- command args...
coreguard --version
coreguard --help
```

`--json` reports the exit status, timeout/resource classification, bounded
output, process metrics, and opt-in Job Object aggregate metrics. A normal
child exit remains distinct from a start failure, timeout, containment failure,
or resource-limit result. Without `--json` no output is captured: the child
inherits the console handles and a one-line summary is written to stderr.

`--version` prints `coreguard <version>` on one line and exits with 0. The
version is the numeric release version of a versioned build
(`-DCOREGUARD_VERSION=major.minor.patch`); a local development build prints
`coreguard dev`.

### CLI exit codes

| Exit code | Meaning |
|-----------|---------|
| `0`-`255` | The controlled process exited normally. The value is the payload exit code truncated to its low 8 bits. |
| `123` | A job-wide resource limit was hit (see `resource_limit_kind`). |
| `124` | The wall-clock timeout `--timeout-ms` expired. |
| `125` | The payload could not be started or could not be contained. |
| `126` | An internal error, including a failed capture reader. |
| `2` | Usage error. Nothing was started. |

These codes are the CLI contract. They are not part of the C API, which
returns `cg_status` values instead.

### CLI JSON result contract

`--json` writes exactly one JSON object to stdout, and nothing to stderr, so a
machine consumer can parse stdout whenever a run was attempted. Rejected
options exit with code `2` before any execution and emit no JSON. Every object
carries `"contract_version": 1`. That number identifies the shape of this
object, not the product version: a new `contract_version` means a field was
removed, renamed, or changed in meaning. Additional fields can be introduced
without a new version, so consumers must ignore unknown keys.

Fields with a fixed meaning:

| Field | Meaning |
|-------|---------|
| `contract_version` | Shape version of this object; currently `1`. |
| `status` | `exited`, `timeout`, `start_failed`, `containment_failed`, `internal_error`, `usage_error`, or `resource_limit`. |
| `exit_code` | Payload exit code, or `null` when the payload did not exit on its own. |
| `timed_out` | The wall-clock timeout expired. |
| `resource_limit_hit`, `resource_limit_kind` | Native limit evidence and its `memory`, `cpu_time`, `active_processes`, or `unknown` cause. |
| `duration_ms` | Milliseconds from the start of the run until the run was classified. The following tree teardown and capture-reader shutdown are not included. |
| `process_id` | Root payload process id. |
| `cleanup_ok` | The controlled tree was confirmed terminated. |
| `applied_limits` | The limits this run was configured with, in native units: `timeout_ms` (always present, includes the 120000 ms default), `memory_limit_bytes`, `cpu_time_limit_ms`, `active_process_limit`. An unset limit is `null`. |
| `metrics_scope`, `metrics` | Root-process metrics; `null` marks an unavailable native value. |
| `job_metrics_scope`, `job_metrics` | Job Object aggregate snapshot, taken while the job handle is still open. |
| `output_truncated` | At least one captured stream exceeded the 1 MiB raw-byte cap. |
| `stdout_size`, `stderr_size` | Retained stream sizes in bytes, after CR/CRLF normalization; the authoritative value for a receipt. A truncated stream reports exactly the retained prefix, not the discarded volume. |
| `stdout`, `stderr` | Captured text. Valid UTF-8 is preserved; an invalid byte is escaped as `\u00XX`, so a byte-exact length must be read from `stdout_size`/`stderr_size`, not from the string. |
| `win32_error` | First Win32 error of the failed operation, present only when non-zero. |

`applied_limits` is the authoritative record of a run's enforcement inputs. A
receipt can bind the outcome to it instead of re-deriving defaults, unit
conversions, or repeated-option handling from the command line. A usage error
is rejected before execution, so an object is only emitted for a run that was
actually attempted.

## Resource limits and metrics

- `--timeout-ms` bounds wall-clock waiting for the controlled Job.
- `--memory-limit-mb` enforces a hard, job-wide committed-memory limit.
- `--cpu-time-limit-ms` enforces a hard, job-wide user-mode CPU-time limit.
  CoreGuard enforces it by polling the job's user-mode accounting in bounded
  intervals and terminating the whole job as soon as the limit is exceeded.
  Windows' own job-time limit stays set as a kernel backstop, but Microsoft
  documents that check as periodic, so it can fire seconds after the limit is
  exceeded. The poll is the primary enforcement path, not the fallback.
- `--max-processes` enforces the maximum number of simultaneously active Job
  processes; the root process counts as one.

Process metrics cover the started root payload. The opt-in
`cg_run_with_job_metrics` API and JSON mode also report Job Object aggregate
metrics where Windows provides them. Validity masks distinguish a measured
zero from an unavailable native value. CPU values are CPU time, not CPU
utilization; memory enforcement is not a private-byte or general sandbox
guarantee.

## Known limitations

- Only Windows x64 with MSVC/UCRT and the tested Release configuration is
  claimed by this source release.
- The library is static-only; consumers must link their executable again when
  upgrading the library.
- Public structures are layout-based. Consumers must recompile when a public
  layout changes.
- There is no stable cross-version ABI guarantee.
- Coreguard does not provide network, disk, UI, scheduler, IPC, or general
  sandbox policy enforcement.
- The archive is unsigned and no package-manager integration is provided.

## Future release verification

`v0.1.0` is a historical unsigned release and does not have a retroactive
GitHub build-provenance attestation. Future tag releases will be built by the
tag-only `.github/workflows/release.yml` workflow, which produces
`SHA256SUMS` and `release-manifest.json` and attests the release ZIP only.

For a future release, compare the ZIP with `SHA256SUMS`, compare the embedded
`bin/coreguard.exe` with `coreguard_exe_sha256` in `release-manifest.json`, and
run:

```powershell
gh attestation verify .\coreguard-X.Y.Z-windows-x64-msvc.zip `
  --repo trungminhdo4-glitch/coreguard `
  --signer-workflow trungminhdo4-glitch/coreguard/.github/workflows/release.yml `
  --source-ref refs/tags/vX.Y.Z
```

The attestation links the digest to the repository, source tag/commit, and
workflow. It is not a guarantee that the code is vulnerability-free. See
[`docs/release-trust.md`](docs/release-trust.md) for the future release flow
and deferred signing, SBOM, and package-manager decisions.

## License

Coreguard is licensed under the MIT License. See [`LICENSE`](LICENSE) for the
complete terms.
