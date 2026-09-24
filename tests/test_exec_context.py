"""Execution-context contract tests for the Coreguard Windows CLI."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CAPTURE_PREFIX_MAX_BYTES = 1024 * 1024 * 1024

ENV_CHILD = (
    "import os, json; print(json.dumps("
    "{k: v for k, v in os.environ.items() if not k.startswith('=')}, "
    "sort_keys=True))"
)
CWD_CHILD = "import os; print(os.getcwd())"
STDIN_DIGEST_CHILD = (
    "import sys, hashlib; d = sys.stdin.buffer.read(); "
    "print(len(d), hashlib.sha256(d).hexdigest())"
)


class ExecContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Coreguard targets Windows")
        configured_exe = os.environ.get("COREGUARD_EXE")
        candidates = (BUILD / "coreguard.exe", BUILD / "Release" / "coreguard.exe")
        cls.exe = (
            pathlib.Path(configured_exe).resolve()
            if configured_exe
            else next(
                (
                    candidate.resolve()
                    for candidate in candidates
                    if candidate.is_file()
                ),
                candidates[0].resolve(),
            )
        )
        if not cls.exe.is_file():
            raise unittest.SkipTest("build coreguard.exe first")

    def run_raw(
        self,
        options: list[str],
        command: list[str],
        *,
        json_mode: bool = True,
        env: dict[str, str] | None = None,
        cwd: pathlib.Path | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runner = [str(self.exe), "run", "--timeout-ms", "10000"]
        if json_mode:
            runner.insert(2, "--json")
        runner.extend(options)
        runner.extend(["--", *command])
        return subprocess.run(
            runner,
            cwd=cwd or ROOT,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
        )

    def run_json(
        self,
        options: list[str],
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: pathlib.Path | None = None,
        input_text: str | None = None,
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        completed = self.run_raw(
            options,
            command,
            json_mode=True,
            env=env,
            cwd=cwd,
            input_text=input_text,
        )
        self.assertEqual(completed.stderr, "", completed.stderr)
        return json.loads(completed.stdout), completed

    def run_plain(
        self,
        options: list[str],
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_raw(options, command, json_mode=False, env=env)

    def child_environment(
        self,
        options: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        payload, completed = self.run_json(
            options, [sys.executable, "-c", ENV_CHILD], env=env
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "exited", payload)
        return json.loads(payload["stdout"])

    def test_cwd_is_applied_to_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cg cwd ä ", dir=BUILD) as temporary:
            expected = pathlib.Path(temporary).resolve()
            payload, completed = self.run_json(
                ["--cwd", temporary], [sys.executable, "-c", CWD_CHILD]
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["status"], "exited", payload)
            self.assertEqual(
                pathlib.Path(payload["stdout"].strip()).resolve(), expected
            )

    def test_cwd_relative_path_resolves_against_caller_cwd(self) -> None:
        payload, completed = self.run_json(
            ["--cwd", "tests"], [sys.executable, "-c", CWD_CHILD]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "exited", payload)
        self.assertEqual(
            pathlib.Path(payload["stdout"].strip()).resolve(),
            (ROOT / "tests").resolve(),
        )

    def test_missing_cwd_reports_start_failure(self) -> None:
        payload, completed = self.run_json(
            ["--cwd", str(BUILD / "cg-missing-directory")],
            [sys.executable, "-c", CWD_CHILD],
        )
        self.assertEqual(completed.returncode, 125, completed.stderr)
        self.assertEqual(payload["status"], "start_failed", payload)
        self.assertEqual(payload["win32_error"], 267)
        self.assertIsNone(payload["exit_code"])

    def test_invalid_cwd_usage_errors(self) -> None:
        for options in (
            ["--cwd", ""],
            ["--cwd", "tests", "--cwd", "tests"],
        ):
            with self.subTest(options=options):
                completed = self.run_raw(options, [sys.executable, "-c", CWD_CHILD])
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("--cwd", completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_env_clear_replaces_environment(self) -> None:
        observed = self.child_environment(
            ["--env-clear", "--env", "A=1", "--env", "B=zwei"]
        )
        self.assertEqual(observed, {"A": "1", "B": "zwei"})

    def test_env_clear_without_entries_is_empty(self) -> None:
        observed = self.child_environment(["--env-clear"])
        self.assertEqual(observed, {})

    def test_env_override_merges_parent_and_sorts(self) -> None:
        environment = os.environ.copy()
        environment["CG_PARENT_MARKER"] = "kept"
        environment["CG_OVERRIDE_MARKER"] = "parent"
        observed = self.child_environment(
            ["--env", "CG_OVERRIDE_MARKER=child"], env=environment
        )
        self.assertEqual(observed.get("CG_PARENT_MARKER"), "kept")
        self.assertEqual(observed.get("CG_OVERRIDE_MARKER"), "child")
        self.assertEqual(list(observed), sorted(observed))

    def test_env_override_matches_case_insensitively(self) -> None:
        environment = os.environ.copy()
        environment["CG_CASE_MARKER"] = "parent"
        observed = self.child_environment(
            ["--env", "cg_case_marker=child"], env=environment
        )
        matching = [key for key in observed if key.upper() == "CG_CASE_MARKER"]
        self.assertEqual(matching, ["CG_CASE_MARKER"])
        self.assertEqual(observed["CG_CASE_MARKER"], "child")

    def test_invalid_env_usage_errors(self) -> None:
        for options in (
            ["--env", "NOEQUALS"],
            ["--env", "=value"],
            ["--env", "A=1", "--env", "a=2"],
        ):
            with self.subTest(options=options):
                completed = self.run_raw(options, [sys.executable, "-c", ENV_CHILD])
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("--env", completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_env_unicode_and_spaces_roundtrip(self) -> None:
        observed = self.child_environment(
            [
                "--env-clear",
                "--env",
                "UNI=grüße",
                "--env",
                "SPACE=a b c",
            ]
        )
        self.assertEqual(observed, {"SPACE": "a b c", "UNI": "grüße"})

    def test_capture_limit_truncates_both_streams(self) -> None:
        child = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('o' * 500); sys.stderr.write('e' * 500)",
        ]
        payload, completed = self.run_json(["--capture-limit-bytes", "64"], child)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "exited", payload)
        self.assertEqual(len(payload["stdout"]), 64)
        self.assertEqual(len(payload["stderr"]), 64)
        self.assertEqual(payload["stdout"], "o" * 64)
        self.assertEqual(payload["stderr"], "e" * 64)
        self.assertTrue(payload["output_truncated"])

    def test_capture_limit_requires_json(self) -> None:
        completed = self.run_plain(
            ["--capture-limit-bytes", "64"],
            [sys.executable, "-c", "print('x')"],
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("--capture-limit-bytes", completed.stderr)

    def test_capture_limit_bounds(self) -> None:
        for value in ("0", str(CAPTURE_PREFIX_MAX_BYTES + 1)):
            with self.subTest(value=value):
                completed = self.run_raw(
                    ["--capture-limit-bytes", value],
                    [sys.executable, "-c", "print('x')"],
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("--capture-limit-bytes", completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_cwd_without_json_runs_child(self) -> None:
        completed = self.run_plain(
            ["--cwd", "tests"], [sys.executable, "-c", CWD_CHILD]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            pathlib.Path(completed.stdout.strip()).resolve(),
            (ROOT / "tests").resolve(),
        )
        self.assertIn("status=exited", completed.stderr)

    def test_combined_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cg-combined-", dir=BUILD) as temporary:
            payload, completed = self.run_json(
                [
                    "--cwd",
                    temporary,
                    "--env-clear",
                    "--env",
                    "ONLY=one",
                    "--capture-limit-bytes",
                    "256",
                ],
                [
                    sys.executable,
                    "-c",
                    "import os, json; print(json.dumps("
                    "{'cwd': os.getcwd(), 'env': "
                    "{k: v for k, v in os.environ.items() "
                    "if not k.startswith('=')}}, sort_keys=True))",
                ],
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["status"], "exited", payload)
            observed = json.loads(payload["stdout"])
            self.assertEqual(
                pathlib.Path(observed["cwd"]).resolve(),
                pathlib.Path(temporary).resolve(),
            )
            self.assertEqual(observed["env"], {"ONLY": "one"})

    def test_stdin_file_is_delivered_exactly(self) -> None:
        payload = bytes(range(256)) * 16
        with tempfile.TemporaryDirectory(prefix="cg-stdin-", dir=BUILD) as temporary:
            path = pathlib.Path(temporary) / "payload.bin"
            path.write_bytes(payload)
            result, completed = self.run_json(
                ["--stdin-file", str(path)],
                [sys.executable, "-c", STDIN_DIGEST_CHILD],
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "exited", result)
        observed = result["stdout"].split()
        self.assertEqual(observed[0], str(len(payload)))
        self.assertEqual(observed[1], hashlib.sha256(payload).hexdigest())

    def test_stdin_max_payload_without_reader_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="cg-stdin-max-", dir=BUILD
        ) as temporary:
            path = pathlib.Path(temporary) / "max.bin"
            path.write_bytes(b"s" * (64 * 1024))
            started = time.monotonic()
            result, completed = self.run_json(
                ["--stdin-file", str(path)],
                ["cmd.exe", "/d", "/c", "exit 0"],
            )
            elapsed = time.monotonic() - started
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "exited", result)
        self.assertLess(elapsed, 10.0)

    def test_stdin_default_inherits_caller_stdin(self) -> None:
        result, completed = self.run_json(
            [],
            [
                sys.executable,
                "-c",
                "import sys; print(repr(sys.stdin.buffer.read()))",
            ],
            input_text="inherit-me\n",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "exited", result)
        self.assertIn("inherit-me", result["stdout"])

    def test_stdin_file_rejects_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="cg-stdin-bad-", dir=BUILD
        ) as temporary:
            base = pathlib.Path(temporary)
            (base / "empty.bin").write_bytes(b"")
            (base / "over.bin").write_bytes(b"x" * (64 * 1024 + 1))
            for path in (
                base / "missing.bin",
                base / "empty.bin",
                base / "over.bin",
            ):
                with self.subTest(path=path.name):
                    completed = self.run_raw(
                        ["--stdin-file", str(path)],
                        [sys.executable, "-c", "pass"],
                    )
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn("--stdin-file", completed.stderr)
                    self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
