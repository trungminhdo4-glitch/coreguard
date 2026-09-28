"""Shared machinery for the frozen public ABI contract gate.

The gate compiles two small TUs with the same MSVC toolchain as the public
consumer tests:

* ``tests/consumers/layout_manifest/main.c`` prints every public
  size/alignment/offset plus the enum and macro values, header-only.
* ``tests/consumers/abi_signature/main.c`` is compile-only and types every
  public function against an exact function pointer.

The frozen contract ``tests/abi/contract_v0.2.0.json`` is compared against the
current header. Changed or removed entries fail; added entries require an
explicit sealed allowlist entry. The seal makes an un-reviewed edit visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONSUMERS = ROOT / "tests" / "consumers"
ABI_DIR = ROOT / "tests" / "abi"
PUBLIC_HEADER = ROOT / "include" / "coreguard.h"
MANIFEST_SOURCE = CONSUMERS / "layout_manifest" / "main.c"
SIGNATURE_SOURCE = CONSUMERS / "abi_signature" / "main.c"
CONTRACT_PATH = ABI_DIR / "contract_v0.2.0.json"

CONTRACT_SCHEMA = "coreguard-abi-contract/v1"
CONTRACT_TARGET = "windows-x64-msvc"
REQUIRED_POINTER_BITS = "64"

VCVARS = os.environ.get("COREGUARD_VCVARS", "vcvars64.bat")
CL_FLAGS = "/nologo /W4 /WX /analyze /wd28301 /MD /std:c11 /TC"
TIMEOUT_SECONDS = 120
PROVENANCE_PREFIX = "provenance("


class AbiGateError(RuntimeError):
    """The ABI gate could not produce a trustworthy observation."""


def vcvars_available() -> bool:
    if "COREGUARD_VCVARS" in os.environ:
        return pathlib.Path(VCVARS).is_file()
    return shutil.which(VCVARS) is not None


def _stage_consumer(
    source: pathlib.Path, root: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    include_dir = root / "include"
    include_dir.mkdir()
    shutil.copy2(PUBLIC_HEADER, include_dir / "coreguard.h")
    staged_source = root / source.name
    shutil.copy2(source, staged_source)
    return include_dir, staged_source


def _run(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def collect_manifest() -> tuple[dict[str, str], dict[str, str], str]:
    """Compile and run the layout manifest; return (entries, provenance, log)."""

    with tempfile.TemporaryDirectory(prefix="coreguard-abi-manifest-") as temporary:
        root = pathlib.Path(temporary)
        include_dir, staged_source = _stage_consumer(MANIFEST_SOURCE, root)
        executable = root / "layout_manifest.exe"
        compiled = _run(
            f'call "{VCVARS}" && cl {CL_FLAGS} '
            f'/I"{include_dir}" "{staged_source}" /Fe:"{executable}"',
            root,
        )
        log = compiled.stdout + compiled.stderr
        if compiled.returncode != 0 or not executable.is_file():
            raise AbiGateError(f"layout manifest did not compile:\n{log}")
        manifest = _run(str(executable), root)
        log += manifest.stdout + manifest.stderr
        if manifest.returncode != 0:
            raise AbiGateError(f"layout manifest did not run:\n{log}")
        entries, provenance = parse_manifest(manifest.stdout)
    if entries.get("pointer_bits") != REQUIRED_POINTER_BITS:
        raise AbiGateError(
            "manifest was produced for pointer_bits="
            f"{entries.get('pointer_bits')!r}, expected {REQUIRED_POINTER_BITS!r}"
        )
    return entries, provenance, log


def compile_signature() -> str:
    """Compile the typed-signature TU with /c; raise AbiGateError on failure."""

    with tempfile.TemporaryDirectory(prefix="coreguard-abi-signature-") as temporary:
        root = pathlib.Path(temporary)
        include_dir, staged_source = _stage_consumer(SIGNATURE_SOURCE, root)
        compiled = _run(
            f'call "{VCVARS}" && cl {CL_FLAGS} /c '
            f'/I"{include_dir}" "{staged_source}" /Fo:"{root / "abi_signature.obj"}"',
            root,
        )
        log = compiled.stdout + compiled.stderr
        if compiled.returncode != 0:
            raise AbiGateError(f"public signature check did not compile:\n{log}")
    return log


def parse_manifest(text: str) -> tuple[dict[str, str], dict[str, str]]:
    entries: dict[str, str] = {}
    provenance: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith(PROVENANCE_PREFIX):
            provenance[key] = value
        else:
            entries[key] = value
    if not entries:
        raise AbiGateError("manifest output contained no entries")
    return entries, provenance


def contract_body(
    entries: dict[str, str],
    additive_allowlist: list[str],
    source: str,
) -> dict[str, object]:
    return {
        "schema": CONTRACT_SCHEMA,
        "target": CONTRACT_TARGET,
        "source": source,
        "entries": {key: entries[key] for key in sorted(entries)},
        "additive_allowlist": sorted(additive_allowlist),
    }


def compute_seal(body: dict[str, object]) -> str:
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_contract(
    path: pathlib.Path = CONTRACT_PATH,
) -> tuple[dict[str, object], str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AbiGateError(f"cannot read frozen contract {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AbiGateError(f"frozen contract {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AbiGateError(f"frozen contract {path} is not a JSON object")
    recorded_seal = data.pop("seal_sha256", None)
    if not isinstance(recorded_seal, str):
        recorded_seal = None
    return data, recorded_seal


def diff_contract(
    observed: dict[str, str],
    contract: dict[str, object],
) -> list[str]:
    """Return human-readable violations; empty means the gate passes."""

    frozen_raw = contract.get("entries")
    if not isinstance(frozen_raw, dict):
        raise AbiGateError("frozen contract has no entries object")
    frozen = {str(key): str(value) for key, value in frozen_raw.items()}
    allowlist_raw = contract.get("additive_allowlist", [])
    if not isinstance(allowlist_raw, list):
        raise AbiGateError("frozen contract additive_allowlist is not a list")
    allowlist = {str(entry) for entry in allowlist_raw}

    problems: list[str] = []
    for key in sorted(set(frozen) - set(observed)):
        problems.append(f"REMOVED {key}: frozen {frozen[key]!r} is gone")
    for key in sorted(set(frozen) & set(observed)):
        if frozen[key] != observed[key]:
            problems.append(
                f"CHANGED {key}: frozen {frozen[key]!r} -> observed {observed[key]!r}"
            )
    for key in sorted(set(observed) - set(frozen)):
        if key not in allowlist:
            problems.append(
                f"ADDED {key}={observed[key]!r} is not in additive_allowlist"
            )
    for key in sorted(allowlist - set(observed)):
        problems.append(f"STALE ALLOWLIST ENTRY {key} is not observed")
    return problems
