# Coreguard v0.1.0

Coreguard v0.1.0 is a Windows x64/MSVC static-library source release.
It provides a direct executable runner with Windows Job Object
containment, bounded timeout handling, resource limits, structured CLI output,
and C/C++ consumer examples.

Highlights:

- Direct `CreateProcessW` launch with Job Object assignment before resume.
- Timeout cleanup with kill-on-close containment.
- Job-wide committed-memory, user-mode CPU-time, and active-process limits.
- Root-process and opt-in aggregate Job Object metrics.
- C API with explicit result ownership and C++ linkage compatibility.
- Relocatable CMake install tree and exact-version CMake package support.
- Release ZIP: `coreguard-0.1.0-windows-x64-msvc.zip`.

The release is static-only and does not promise a stable cross-version ABI.
The supported distribution scope is Windows x64 with MSVC/UCRT in a Release
configuration. The archive is unsigned.

License: PolyForm Strict License 1.0.0. The v0.1.0 tag and its published
release ship the PolyForm Strict 1.0.0 `LICENSE`; Coreguard v0.1.0 was
source-available, not open source. The current `main` source tree is licensed
under MIT (see the repository `LICENSE` file).
