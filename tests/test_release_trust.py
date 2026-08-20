from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_trust import (  # noqa: E402
    EXPECTED_PACKAGE_FILES,
    ReleaseTrustError,
    expected_archive_name,
    release_version_from_tag,
    validate_version,
    validate_configured_build,
    validate_package,
    verify_sha256sums,
    verify_release_manifest,
    write_release_evidence,
)


VERSION = "1.2.3"
TAG = "v1.2.3"
COMMIT = "a" * 40


def make_archive(path: pathlib.Path, *, version: str = VERSION, extra: str | None = None) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(EXPECTED_PACKAGE_FILES):
            content = f"fixture:{name}".encode("utf-8")
            if name.endswith("coreguardConfigVersion.cmake"):
                content = f'set(PACKAGE_VERSION "{version}")\n'.encode("utf-8")
            archive.writestr(name, content)
        if extra is not None:
            archive.writestr(extra, b"unexpected fixture")


class ReleaseTrustTests(unittest.TestCase):
    def test_synthetic_future_version_contract(self) -> None:
        self.assertEqual(validate_version("99.99.99"), "99.99.99")
        with self.assertRaisesRegex(ReleaseTrustError, "version"):
            validate_version("99.99")

    def test_tag_contract(self) -> None:
        self.assertEqual(release_version_from_tag(TAG), VERSION)
        with self.assertRaises(ReleaseTrustError):
            release_version_from_tag("0.1.1")
        with self.assertRaises(ReleaseTrustError):
            release_version_from_tag("v1.2")

    def test_package_layout_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / expected_archive_name(VERSION)
            make_archive(archive)
            summary = validate_package(archive, VERSION)
            self.assertEqual(summary.archive_name, expected_archive_name(VERSION))
            self.assertEqual(len(summary.entries), len(EXPECTED_PACKAGE_FILES))
            self.assertEqual(len(summary.archive_sha256), 64)
            self.assertEqual(
                summary.coreguard_exe_sha256,
                hashlib.sha256(b"fixture:bin/coreguard.exe").hexdigest(),
            )

    def test_unexpected_package_content_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / expected_archive_name(VERSION)
            make_archive(archive, extra="build/debug.pdb")
            with self.assertRaisesRegex(ReleaseTrustError, "unexpected"):
                validate_package(archive, VERSION)

    def test_package_version_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / expected_archive_name(VERSION)
            make_archive(archive, version="1.2.4")
            with self.assertRaisesRegex(ReleaseTrustError, "CMake package version mismatch"):
                validate_package(archive, VERSION)

    def test_configured_version_and_cpack_name_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            build = root / "build"
            build.mkdir()
            archive = root / expected_archive_name(VERSION)
            make_archive(archive)
            (build / "CMakeCache.txt").write_text(
                f"COREGUARD_VERSION:STRING={VERSION}\n", encoding="utf-8"
            )
            (build / "CPackConfig.cmake").write_text(
                "\n".join(
                    [
                        f'set(CPACK_PACKAGE_VERSION "{VERSION}")',
                        f'set(CPACK_PACKAGE_FILE_NAME "{expected_archive_name(VERSION)[:-4]}")',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = validate_configured_build(build, VERSION, archive)
            self.assertEqual(result["coreguard_version"], VERSION)
            (build / "CMakeCache.txt").write_text(
                "COREGUARD_VERSION:STRING=1.2.4\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ReleaseTrustError, "COREGUARD_VERSION mismatch"):
                validate_configured_build(build, VERSION, archive)

    def test_sha256sums_and_tamper_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / expected_archive_name(VERSION)
            make_archive(archive)
            output_a = root / "evidence-a"
            result = write_release_evidence(
                archive_path=archive,
                tag=TAG,
                commit_sha=COMMIT,
                output_dir=output_a,
                timestamp="2026-08-20T10:00:00Z",
            )
            self.assertEqual(verify_sha256sums(output_a / "SHA256SUMS", archive), result["zip_sha256"])
            self.assertEqual(
                verify_release_manifest(
                    output_a / "release-manifest.json",
                    output_a / "SHA256SUMS",
                    archive,
                    version=VERSION,
                    commit_sha=COMMIT,
                )["status"],
                "PASS",
            )

            tampered = root / "tampered" / archive.name
            tampered.parent.mkdir()
            shutil.copy2(archive, tampered)
            data = bytearray(tampered.read_bytes())
            data[-1] ^= 0x01
            tampered.write_bytes(data)
            with self.assertRaisesRegex(ReleaseTrustError, "SHA-256 mismatch"):
                verify_sha256sums(output_a / "SHA256SUMS", tampered)

    def test_manifest_is_deterministic_for_fixed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / expected_archive_name(VERSION)
            make_archive(archive)
            timestamp = "2026-08-20T10:00:00Z"
            first = write_release_evidence(
                archive_path=archive,
                tag=TAG,
                commit_sha=COMMIT,
                output_dir=root / "first",
                timestamp=timestamp,
            )
            second = write_release_evidence(
                archive_path=archive,
                tag=TAG,
                commit_sha=COMMIT,
                output_dir=root / "second",
                timestamp=timestamp,
            )
            self.assertEqual(
                (root / "first" / "release-manifest.json").read_bytes(),
                (root / "second" / "release-manifest.json").read_bytes(),
            )
            manifest = json.loads((root / "first" / "release-manifest.json").read_text())
            self.assertEqual(manifest["commit_sha"], COMMIT)
            self.assertEqual(manifest["coreguard_exe_sha256"], first["coreguard_exe_sha256"])
            self.assertEqual(first["zip_sha256"], second["zip_sha256"])

    def test_dry_run_evidence_has_no_tag_or_attestation_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            version = "99.99.99"
            archive = root / expected_archive_name(version)
            make_archive(archive, version=version)
            output = root / "dry-run"
            write_release_evidence(
                archive_path=archive,
                version=version,
                commit_sha=COMMIT,
                output_dir=output,
                timestamp="2026-08-20T10:00:00Z",
                dry_run=True,
            )
            manifest = json.loads((output / "release-manifest.json").read_text())
            self.assertIsNone(manifest["git_tag"])
            self.assertEqual(manifest["release_mode"], "dry-run-validation")
            self.assertEqual(manifest["provenance"]["attestation"], "not generated")
            self.assertEqual(
                verify_release_manifest(
                    output / "release-manifest.json",
                    output / "SHA256SUMS",
                    archive,
                    version=version,
                    commit_sha=COMMIT,
                    dry_run=True,
                )["status"],
                "PASS",
            )

    def test_workflow_is_tag_only_and_owner_gated_for_publication(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("pull_request:", workflow)
        self.assertIn(
            "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d", workflow
        )
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("inputs.publish == true", workflow)
        self.assertIn("release-publication", workflow)
        self.assertIn('RELEASE_TAG -eq "v0.1.0"', workflow)


if __name__ == "__main__":
    unittest.main()
