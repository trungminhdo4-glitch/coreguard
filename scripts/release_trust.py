"""Release package, version, and digest gates for future Coreguard tags.

This module intentionally uses only the Python standard library.  It does not
create tags, GitHub Releases, or attestations; those actions remain workflow
and owner-gated responsibilities.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import pathlib
import re
import sys
import zipfile
from dataclasses import dataclass


REPOSITORY = "trungminhdo4-glitch/coreguard"
WORKFLOW_PATH = ".github/workflows/release.yml"
TAG_PATTERN = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

EXPECTED_PACKAGE_FILES = frozenset(
    {
        "bin/coreguard.exe",
        "include/coreguard.h",
        "lib/coreguard.lib",
        "lib/cmake/coreguard/coreguardConfig.cmake",
        "lib/cmake/coreguard/coreguardConfigVersion.cmake",
        "lib/cmake/coreguard/coreguardTargets.cmake",
        "lib/cmake/coreguard/coreguardTargets-release.cmake",
        "LICENSE",
    }
)


class ReleaseTrustError(RuntimeError):
    """Raised when a release trust gate must fail closed."""


@dataclass(frozen=True)
class PackageSummary:
    archive_path: pathlib.Path
    archive_name: str
    archive_sha256: str
    coreguard_exe_sha256: str
    entries: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseTrustError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def release_version_from_tag(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise ReleaseTrustError(
            f"release ref must match vX.Y.Z exactly, received {tag!r}"
        )
    return validate_version(match.group("version"))


def validate_version(version: str) -> str:
    """Validate a numeric semantic version without asserting a Git tag exists."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseTrustError(
            f"version must match X.Y.Z exactly, received {version!r}"
        )
    return version


def expected_archive_name(version: str) -> str:
    return f"coreguard-{version}-windows-x64-msvc.zip"


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseTrustError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ReleaseTrustError(f"{path} is not UTF-8 text") from exc


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not name or info.is_dir():
        raise ReleaseTrustError(f"directory or empty ZIP member is not allowed: {name!r}")
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ReleaseTrustError(f"non-portable or absolute ZIP member: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseTrustError(f"unsafe ZIP member path: {name!r}")
    # ZIP can encode symlinks through Unix external attributes.  A release
    # contract must contain regular files only.
    unix_type = (info.external_attr >> 16) & 0o170000
    if unix_type == 0o120000:
        raise ReleaseTrustError(f"symlink ZIP member is not allowed: {name!r}")
    return name


def _package_version_from_config(config_text: str, path: str) -> str:
    match = re.search(r'set\(PACKAGE_VERSION\s+"([^"]+)"\)', config_text)
    if not match:
        raise ReleaseTrustError(f"{path} does not declare PACKAGE_VERSION")
    return match.group(1)


def validate_package(archive_path: pathlib.Path, version: str | None = None) -> PackageSummary:
    """Validate the exact install/archive contract and return its digests."""

    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise ReleaseTrustError(f"release archive does not exist: {archive_path}")
    if version is not None and archive_path.name != expected_archive_name(version):
        raise ReleaseTrustError(
            f"archive filename mismatch: expected {expected_archive_name(version)}, "
            f"received {archive_path.name}"
        )

    try:
        with zipfile.ZipFile(archive_path) as archive:
            names: list[str] = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _zip_member_name(info)
                if name in names:
                    raise ReleaseTrustError(f"duplicate ZIP member: {name}")
                names.append(name)
            actual = set(names)
            missing = sorted(EXPECTED_PACKAGE_FILES - actual)
            unexpected = sorted(actual - EXPECTED_PACKAGE_FILES)
            if missing or unexpected:
                details = []
                if missing:
                    details.append(f"missing={missing}")
                if unexpected:
                    details.append(f"unexpected={unexpected}")
                raise ReleaseTrustError("release ZIP contract mismatch: " + "; ".join(details))

            executable = archive.read("bin/coreguard.exe")
            license_bytes = archive.read("LICENSE")
            if not executable:
                raise ReleaseTrustError("bin/coreguard.exe is empty")
            if not license_bytes:
                raise ReleaseTrustError("LICENSE is empty")
            if version is not None:
                config_version = _package_version_from_config(
                    archive.read("lib/cmake/coreguard/coreguardConfigVersion.cmake").decode(
                        "utf-8"
                    ),
                    "lib/cmake/coreguard/coreguardConfigVersion.cmake",
                )
                if config_version != version:
                    raise ReleaseTrustError(
                        "CMake package version mismatch: "
                        f"expected {version}, received {config_version}"
                    )
    except zipfile.BadZipFile as exc:
        raise ReleaseTrustError(f"invalid ZIP archive: {archive_path}") from exc
    except KeyError as exc:
        raise ReleaseTrustError(f"required ZIP member is unreadable: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ReleaseTrustError("CMake package version file is not UTF-8") from exc

    return PackageSummary(
        archive_path=archive_path,
        archive_name=archive_path.name,
        archive_sha256=sha256_file(archive_path),
        coreguard_exe_sha256=sha256_bytes(executable),
        entries=tuple(sorted(names)),
    )


def _cmake_cache_value(cache_text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:[^=]*=(.*)$", cache_text, re.MULTILINE)
    if not match:
        raise ReleaseTrustError(f"CMake cache does not define {key}")
    return match.group(1).strip()


def validate_configured_build(
    build_dir: pathlib.Path, version: str, archive_path: pathlib.Path
) -> dict[str, str]:
    """Check CMake/CPack outputs before a release archive is attested."""

    validate_version(version)
    build_dir = build_dir.resolve()
    cache_text = _read_text(build_dir / "CMakeCache.txt")
    configured_version = _cmake_cache_value(cache_text, "COREGUARD_VERSION")
    if configured_version != version:
        raise ReleaseTrustError(
            f"COREGUARD_VERSION mismatch: expected {version}, received {configured_version}"
        )

    cpack_text = _read_text(build_dir / "CPackConfig.cmake")
    expected_name = expected_archive_name(version).removesuffix(".zip")
    package_version = re.search(
        r'set\(CPACK_PACKAGE_VERSION\s+"([^"]+)"\)', cpack_text
    )
    package_name = re.search(
        r'set\(CPACK_PACKAGE_FILE_NAME\s+"([^"]+)"\)', cpack_text
    )
    if not package_version or package_version.group(1) != version:
        received = package_version.group(1) if package_version else "<absent>"
        raise ReleaseTrustError(
            f"CPACK_PACKAGE_VERSION mismatch: expected {version}, received {received}"
        )
    if not package_name or package_name.group(1) != expected_name:
        received = package_name.group(1) if package_name else "<absent>"
        raise ReleaseTrustError(
            f"CPACK_PACKAGE_FILE_NAME mismatch: expected {expected_name}, received {received}"
        )

    artifact = pathlib.Path(archive_path).resolve()
    if artifact.name != expected_archive_name(version):
        raise ReleaseTrustError(
            f"release artifact filename mismatch: expected {expected_archive_name(version)}, "
            f"received {artifact.name}"
        )
    if not artifact.is_file():
        raise ReleaseTrustError(f"release artifact does not exist: {artifact}")

    return {
        "coreguard_version": configured_version,
        "cpack_package_version": package_version.group(1),
        "cpack_package_file_name": package_name.group(1),
        "artifact_filename": artifact.name,
    }


def _default_timestamp() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def write_release_evidence(
    *,
    archive_path: pathlib.Path,
    tag: str | None = None,
    version: str | None = None,
    commit_sha: str,
    output_dir: pathlib.Path,
    timestamp: str | None = None,
    repository: str = REPOSITORY,
    workflow_path: str = WORKFLOW_PATH,
    dry_run: bool = False,
) -> dict[str, object]:
    """Write deterministic-for-fixed-input release evidence files."""

    if dry_run:
        if tag is not None:
            raise ReleaseTrustError("dry-run evidence cannot carry a Git tag")
        if version is None:
            raise ReleaseTrustError("dry-run evidence requires a synthetic version")
        version = validate_version(version)
        git_tag: str | None = None
    else:
        if tag is None:
            raise ReleaseTrustError("release evidence requires a Git tag")
        tagged_version = release_version_from_tag(tag)
        if version is not None and validate_version(version) != tagged_version:
            raise ReleaseTrustError(
                f"evidence version mismatch: tag carries {tagged_version}, received {version}"
            )
        version = tagged_version
        git_tag = tag

    if not COMMIT_PATTERN.fullmatch(commit_sha):
        raise ReleaseTrustError("commit SHA must be a full 40-character hexadecimal SHA")
    summary = validate_package(archive_path, version)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or _default_timestamp()
    sha_path = output_dir / "SHA256SUMS"
    manifest_path = output_dir / "release-manifest.json"
    sha_path.write_text(
        f"{summary.archive_sha256}  {summary.archive_name}\n", encoding="utf-8"
    )
    release_mode = "dry-run-validation" if dry_run else "release"
    verification_command = (
        "not generated in PR-safe validation"
        if dry_run
        else (
            f"gh attestation verify {summary.archive_name} --repo {repository} "
            f"--signer-workflow {repository}/{workflow_path} "
            f"--source-ref refs/tags/{tag}"
        )
    )
    manifest: dict[str, object] = {
        "manifest_schema_version": 1,
        "project": "coreguard",
        "version": version,
        "git_tag": git_tag,
        "release_mode": release_mode,
        "commit_sha": commit_sha.lower(),
        "build_platform": "Windows",
        "architecture": "x64",
        "toolchain": "MSVC",
        "configuration": "Release",
        "artifact_filename": summary.archive_name,
        "zip_sha256": summary.archive_sha256,
        "coreguard_exe_path": "bin/coreguard.exe",
        "coreguard_exe_sha256": summary.coreguard_exe_sha256,
        "artifact_entries": list(summary.entries),
        "timestamp": timestamp,
        "provenance": {
            "repository": repository,
            "workflow": workflow_path,
            "predicate_type": "https://slsa.dev/provenance/v1",
            "verification_command": verification_command,
            "attestation": "not generated" if dry_run else "github-artifact-attestation",
        },
        "publication": "not attempted" if dry_run else "owner-gated workflow step",
        "historical_v0_1_0_attestation": "not asserted by this workflow",
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "sha256sums": str(sha_path),
        "release_manifest": str(manifest_path),
        "archive": summary.archive_name,
        "zip_sha256": summary.archive_sha256,
        "coreguard_exe_sha256": summary.coreguard_exe_sha256,
    }


def verify_sha256sums(checksums_path: pathlib.Path, artifact_path: pathlib.Path) -> str:
    """Verify the generated SHA256SUMS file against one downloaded archive."""

    lines = [
        line.strip()
        for line in _read_text(checksums_path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise ReleaseTrustError("SHA256SUMS must contain exactly one artifact entry")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s{2}(.+)", lines[0])
    if not match or not SHA256_PATTERN.fullmatch(match.group(1)):
        raise ReleaseTrustError("SHA256SUMS has an invalid sha256sum entry")
    expected_digest, expected_name = match.groups()
    artifact = pathlib.Path(artifact_path).resolve()
    if artifact.name != expected_name:
        raise ReleaseTrustError(
            f"SHA256SUMS filename mismatch: expected {expected_name}, received {artifact.name}"
        )
    actual_digest = sha256_file(artifact)
    if actual_digest.lower() != expected_digest.lower():
        raise ReleaseTrustError(
            f"SHA-256 mismatch for {artifact.name}: expected {expected_digest}, received {actual_digest}"
        )
    return actual_digest


def verify_release_manifest(
    manifest_path: pathlib.Path,
    checksums_path: pathlib.Path,
    artifact_path: pathlib.Path,
    *,
    version: str,
    commit_sha: str,
    dry_run: bool = False,
) -> dict[str, str]:
    """Verify manifest, package inventory, and checksum evidence as one gate."""

    version = validate_version(version)
    if not COMMIT_PATTERN.fullmatch(commit_sha):
        raise ReleaseTrustError("commit SHA must be a full 40-character hexadecimal SHA")
    summary = validate_package(artifact_path, version)
    verified_sha = verify_sha256sums(checksums_path, artifact_path)
    try:
        manifest = json.loads(_read_text(manifest_path))
    except json.JSONDecodeError as exc:
        raise ReleaseTrustError(f"release manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseTrustError("release manifest must contain a JSON object")

    expected_tag = None if dry_run else f"v{version}"
    expected_mode = "dry-run-validation" if dry_run else "release"
    checks = {
        "manifest_schema_version": 1,
        "project": "coreguard",
        "version": version,
        "git_tag": expected_tag,
        "release_mode": expected_mode,
        "commit_sha": commit_sha.lower(),
        "artifact_filename": summary.archive_name,
        "zip_sha256": verified_sha,
        "coreguard_exe_sha256": summary.coreguard_exe_sha256,
        "artifact_entries": list(summary.entries),
        "historical_v0_1_0_attestation": "not asserted by this workflow",
    }
    for key, expected in checks.items():
        if manifest.get(key) != expected:
            raise ReleaseTrustError(
                f"release manifest mismatch for {key}: expected {expected!r}, "
                f"received {manifest.get(key)!r}"
            )

    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise ReleaseTrustError("release manifest provenance is missing")
    expected_attestation = "not generated" if dry_run else "github-artifact-attestation"
    if provenance.get("attestation") != expected_attestation:
        raise ReleaseTrustError(
            "release manifest attestation state does not match the workflow mode"
        )
    if dry_run and manifest.get("publication") != "not attempted":
        raise ReleaseTrustError("dry-run manifest must state that publication was not attempted")
    return {"status": "PASS", "mode": expected_mode, "version": version}


def _summary(summary: PackageSummary) -> dict[str, object]:
    return {
        "status": "PASS",
        "archive": summary.archive_name,
        "zip_sha256": summary.archive_sha256,
        "coreguard_exe_sha256": summary.coreguard_exe_sha256,
        "entries": list(summary.entries),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    tag_parser = subparsers.add_parser("validate-tag")
    tag_parser.add_argument("--tag", required=True)

    version_parser = subparsers.add_parser("validate-version")
    version_parser.add_argument("--version", required=True)

    build_parser = subparsers.add_parser("validate-build")
    build_version = build_parser.add_mutually_exclusive_group(required=True)
    build_version.add_argument("--tag")
    build_version.add_argument("--version")
    build_parser.add_argument("--build-dir", type=pathlib.Path, required=True)
    build_parser.add_argument("--artifact", type=pathlib.Path, required=True)

    package_parser = subparsers.add_parser("validate-package")
    package_parser.add_argument("--artifact", type=pathlib.Path, required=True)
    package_parser.add_argument("--version")

    evidence_parser = subparsers.add_parser("write-evidence")
    evidence_parser.add_argument("--artifact", type=pathlib.Path, required=True)
    evidence_version = evidence_parser.add_mutually_exclusive_group(required=True)
    evidence_version.add_argument("--tag")
    evidence_version.add_argument("--version")
    evidence_parser.add_argument("--commit", required=True)
    evidence_parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    evidence_parser.add_argument("--timestamp")
    evidence_parser.add_argument("--repository", default=REPOSITORY)
    evidence_parser.add_argument("--workflow", default=WORKFLOW_PATH)
    evidence_parser.add_argument("--dry-run", action="store_true")

    sums_parser = subparsers.add_parser("verify-sha256sums")
    sums_parser.add_argument("--checksums", type=pathlib.Path, required=True)
    sums_parser.add_argument("--artifact", type=pathlib.Path, required=True)

    manifest_parser = subparsers.add_parser("verify-manifest")
    manifest_parser.add_argument("--manifest", type=pathlib.Path, required=True)
    manifest_parser.add_argument("--checksums", type=pathlib.Path, required=True)
    manifest_parser.add_argument("--artifact", type=pathlib.Path, required=True)
    manifest_parser.add_argument("--version", required=True)
    manifest_parser.add_argument("--commit", required=True)
    manifest_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-tag":
            print(json.dumps({"status": "PASS", "version": release_version_from_tag(args.tag)}))
        elif args.command == "validate-version":
            print(json.dumps({"status": "PASS", "version": validate_version(args.version)}))
        elif args.command == "validate-build":
            version = (
                release_version_from_tag(args.tag)
                if args.tag is not None
                else validate_version(args.version)
            )
            result = validate_configured_build(args.build_dir, version, args.artifact)
            print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        elif args.command == "validate-package":
            version = validate_version(args.version) if args.version else None
            print(json.dumps(_summary(validate_package(args.artifact, version)), sort_keys=True))
        elif args.command == "write-evidence":
            result = write_release_evidence(
                archive_path=args.artifact,
                tag=args.tag,
                version=args.version,
                commit_sha=args.commit,
                output_dir=args.output_dir,
                timestamp=args.timestamp,
                repository=args.repository,
                workflow_path=args.workflow,
                dry_run=args.dry_run,
            )
            print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        elif args.command == "verify-sha256sums":
            digest = verify_sha256sums(args.checksums, args.artifact)
            print(json.dumps({"status": "PASS", "sha256": digest}))
        elif args.command == "verify-manifest":
            result = verify_release_manifest(
                args.manifest,
                args.checksums,
                args.artifact,
                version=args.version,
                commit_sha=args.commit,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, sort_keys=True))
        else:  # pragma: no cover - argparse enforces this
            raise ReleaseTrustError(f"unknown command: {args.command}")
    except ReleaseTrustError as exc:
        print(f"RELEASE_TRUST_FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
