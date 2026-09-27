# Coreguard v0.2.0

Coreguard v0.2.0 is a Windows x64/MSVC source release of the bounded execution
layer. It starts one executable directly, places it in a Windows Job Object
before resuming it, and reports bounded execution results as a C API or as a
command-line JSON result.

## Added

- Execution context for the `cg_run_ex` and `cg_run_ex_with_job_metrics`
  variants: `cg_exec_context` carries a child working directory, a complete
  UTF-16 environment block, and a per-run capture prefix.
- CLI parity for the execution context: `--cwd`, `--env-clear`,
  `--env NAME=VALUE`, and `--capture-limit-bytes N`.
- Bounded capture: each stream retains a prefix (default 1 MiB, up to 1 GiB)
  while excess bytes are drained and discarded; `output_truncated` reports
  limit hits.
- Runtime build identity: `coreguard --version` prints `coreguard X.Y.Z` for
  versioned builds and `coreguard dev` for development builds. The identity is
  compiled in; there is no Git lookup, sidecar file, or network access.
- Release provenance and trust tooling: `SHA256SUMS` and a
  `release-manifest.json` that bind the release ZIP and its embedded
  `bin/coreguard.exe` to the tag commit, plus a tag-only GitHub Actions
  attestation path with owner-gated publication.

## Changed

- The current source and release license is MIT (adopted after v0.1.0).
- Release trust is a single-source pipeline (`scripts/release_trust.py` and
  `scripts/windows_release_trust.ps1`): tag/version validation, CMake/CPack
  version coherence, package inventory, runtime identity, packaged-executable
  identity, checksums, and manifest verification.
- A versioned build installs an exact-version `coreguardConfigVersion.cmake`,
  so `find_package(coreguard <X.Y.Z> EXACT CONFIG REQUIRED)` rejects other
  versions.
- Captured output is held in bounded memory instead of temporary files.

## Fixed

- CPU-time limits are enforced while the job runs instead of only being
  classified after a natural exit or after the kernel's late end-of-job
  backstop.
- `cleanup_ok` reports the verified final state for exited runs.
- Child handles are released after spawn and child handle inheritance is
  restricted to the intended set.
- Active-process limit handling preserves handled spawns and polls limit
  notifications correctly.
- CLI limit parsing rejects malformed, overflowing, and zero values, including
  the infinite-timeout sentinel.
- Captured output is bounded and excess bytes are drained instead of being
  accumulated.
- Release trust gates fail closed on package, checksum, and manifest drift.

## Compatibility

- Supported: Windows x64, MSVC/UCRT, Release configuration, static library.
- The new declarations (`cg_exec_context`, `cg_run_ex`,
  `cg_run_ex_with_job_metrics`, and the capture-limit macros) are additive to
  the v0.1.0 public header.
- Existing public structs keep their v0.1.0 layouts; the header's ABI static
  assertions for them are unchanged.
- There is no stable cross-version ABI guarantee. Consumers must recompile
  when a public layout changes and should rebuild for v0.2.0.
- Existing CLI options and JSON keys are unchanged; the new options are
  additive. `coreguard --version` is new and can be feature-detected from its
  exit code and exact output line.

## Known limitations

- Coreguard is a Windows-native bounded execution/containment primitive, not a
  security sandbox: network, disk, UI, scheduler, and IPC policy enforcement
  are out of scope.
- stdin is not supported (deferred).
- JSON Evidence extensions are deferred.
- The Windows process exit channel carries only conventional process-exit
  semantics; a child exit code such as `256` can appear as `0` to the calling
  shell or process. The structured `--json` result always reports the full
  `exit_code` and is authoritative for the child exit status.
- The archive is unsigned (no Authenticode signing) and no package-manager
  integration is provided.

## License

- v0.1.0 is historical and was released under the PolyForm Strict License
  1.0.0.
- v0.2.0 and the current source tree are licensed under MIT.

## Verification

- `SHA256SUMS` covers the release ZIP.
- `release-manifest.json` records the ZIP digest, the embedded
  `bin/coreguard.exe` digest, the artifact inventory, the tag, and the commit.
- GitHub Artifact Attestation covers the ZIP and the evidence files; it is
  produced by the tag-only release workflow, and publication is owner-gated.
- The release trust pipeline executes the built and the packaged CLI and
  verifies `coreguard --version` against the configured version and the
  packaged executable digest.
