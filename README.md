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
```

`--json` reports the exit status, timeout/resource classification, bounded
output, process metrics, and opt-in Job Object aggregate metrics. A normal
child exit remains distinct from a start failure, timeout, containment failure,
or resource-limit result.

## Resource limits and metrics

- `--timeout-ms` bounds wall-clock waiting for the controlled Job.
- `--memory-limit-mb` enforces a hard, job-wide committed-memory limit.
- `--cpu-time-limit-ms` enforces a hard, job-wide user-mode CPU-time limit.
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

Coreguard is source-available, not open source, under the PolyForm Strict License 1.0.0.
The complete license text is in `LICENSE`. Commercial licensing
remains separately available from the copyright holder. No MIT, Apache, GPL,
or public-domain license is granted by this repository.
