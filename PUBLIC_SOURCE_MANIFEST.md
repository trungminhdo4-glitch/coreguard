# Public source manifest

This candidate is a fresh source snapshot for Coreguard v0.1.0. It contains
only files intended for public source consumption:

- `LICENSE`
- `README.md`
- `PUBLIC_SOURCE_MANIFEST.md`
- `CMakeLists.txt`
- `build-msvc.bat`
- `cmake/`
- `include/`
- `src/`
- `tests/`
- `docs/release-notes-v0.1.0.md`

The snapshot intentionally excludes private Git history, `.git/`, build
directories, compiler outputs, caches, logs, bundles, RC working directories,
temporary files, private configuration, credentials, and internal evidence or
handoff documents.

The tests are included because they exercise the public C API, C++ linkage,
resource behavior, install/package behavior, exact-version package matching,
relocation, and clean-room consumption. The optional differential oracle is
not required for the self-contained public verification run.
