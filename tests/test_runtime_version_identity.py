"""End-to-end runtime version identity contract for the built CLI (K1).

The contract is build-time identity only: ``coreguard --version`` prints the
version the binary was configured with (``dev`` for development builds).  No
Git lookup, no sidecar file, no network, and no child process.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT / "scripts"))

from release_trust import (  # noqa: E402
    ReleaseTrustError,
    verify_runtime_identity,
)

CLI_TIMEOUT_SECONDS = 30
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def discover_executable() -> pathlib.Path | None:
    configured = os.environ.get("COREGUARD_EXE")
    if configured:
        candidate = pathlib.Path(configured).resolve()
        return candidate if candidate.is_file() else None
    candidates = (BUILD / "coreguard.exe", BUILD / "Release" / "coreguard.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def configured_version(executable: pathlib.Path) -> str:
    """The version CMake was configured with; absence means a dev build.

    The cache sits next to the executable (single-config generators) or one
    directory above it (Visual Studio ``Release/`` layout).
    """

    for candidate in (executable.parent, executable.parent.parent):
        cache = candidate / "CMakeCache.txt"
        if cache.is_file():
            match = re.search(
                r"^COREGUARD_VERSION:STRING=(.*)$",
                cache.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if match is not None and VERSION_PATTERN.fullmatch(
                match.group(1).strip()
            ):
                return match.group(1).strip()
    return "dev"


class RuntimeVersionIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("coreguard targets Windows")
        cls.exe = discover_executable()
        if cls.exe is None:
            raise unittest.SkipTest("build coreguard.exe first")
        cls.version = configured_version(cls.exe)

    def cli(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(self.exe), *args],
            capture_output=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        )

    def expected_bytes(self) -> bytes:
        # Windows CRT text mode terminates the printf line with CRLF.
        return f"coreguard {self.version}\r\n".encode("ascii")

    def test_version_reports_the_configured_identity(self) -> None:
        completed = self.cli("--version")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, self.expected_bytes())
        self.assertEqual(completed.stderr, b"")

    def test_version_is_side_effect_free_and_mirrors_help_arg_handling(self) -> None:
        baseline = self.cli("--version")
        trailing = self.cli("--version", "garbage")
        self.assertEqual(
            (trailing.returncode, trailing.stdout, trailing.stderr),
            (baseline.returncode, baseline.stdout, baseline.stderr),
        )

    def test_version_creates_no_child_process(self) -> None:
        completed = subprocess.run(
            [
                str(self.exe),
                "run",
                "--json",
                "--timeout-ms",
                "5000",
                "--",
                str(self.exe),
                "--version",
            ],
            capture_output=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "exited")
        self.assertEqual(payload["exit_code"], 0)
        # The capture path normalizes CRLF to LF (existing product behavior).
        self.assertEqual(payload["stdout"], f"coreguard {self.version}\n")
        self.assertEqual(
            payload["job_metrics"]["total_processes"],
            1,
            "coreguard --version must not create a child process",
        )

    def test_help_and_usage_contract_unchanged(self) -> None:
        no_args = self.cli()
        self.assertEqual(no_args.returncode, 2)
        self.assertIn(b"Usage: coreguard run", no_args.stdout)
        self.assertEqual(no_args.stderr, b"")

        help_result = self.cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn(b"coreguard --version", help_result.stdout)
        self.assertEqual(help_result.stderr, b"")

        run_version = self.cli("run", "--version")
        self.assertEqual(run_version.returncode, 2)
        self.assertEqual(run_version.stdout, b"")
        self.assertIn(b"unknown or incomplete option", run_version.stderr)

    def test_release_trust_gate_accepts_only_the_exact_identity(self) -> None:
        wrong_version = "0.0.1" if self.version != "0.0.1" else "0.0.2"
        with self.assertRaises(ReleaseTrustError):
            verify_runtime_identity(self.exe, wrong_version)
        if VERSION_PATTERN.fullmatch(self.version):
            result = verify_runtime_identity(self.exe, self.version)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["runtime_identity"], f"coreguard {self.version}")
        else:
            # A development identity must never pass the release gate.
            with self.assertRaises(ReleaseTrustError):
                verify_runtime_identity(self.exe, self.version)


if __name__ == "__main__":
    unittest.main()
