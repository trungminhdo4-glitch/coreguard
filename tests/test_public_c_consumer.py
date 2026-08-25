"""Clean-room C ABI and public-consumer proofs for the supported MSVC x64 model."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CONSUMERS = ROOT / "tests" / "consumers"
PUBLIC_HEADER = ROOT / "include" / "coreguard.h"
PRE_RESOURCE_HEADER = CONSUMERS / "compatibility_pre_resource" / "coreguard.h"
RESOURCE_LIMIT_HEADER = CONSUMERS / "compatibility_resource_limits" / "coreguard.h"
VCVARS = os.environ.get("COREGUARD_VCVARS", "vcvars64.bat")


class PublicConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.library = next(
            (
                candidate
                for candidate in (
                    BUILD / "coreguard.lib",
                    BUILD / "Release" / "coreguard.lib",
                )
                if candidate.is_file()
            ),
            None,
        )
        if cls.library is None:
            raise unittest.SkipTest(
                "missing public static library; run build-msvc.bat or CMake first"
            )
        if not PUBLIC_HEADER.is_file():
            raise unittest.SkipTest("missing public header")
        if "COREGUARD_VCVARS" in os.environ:
            vcvars_available = pathlib.Path(VCVARS).is_file()
        else:
            vcvars_available = shutil.which(VCVARS) is not None
        if not vcvars_available:
            raise unittest.SkipTest(
                "missing vcvars64.bat; set COREGUARD_VCVARS or add it to PATH"
            )

    def stage(
        self,
        source: pathlib.Path,
        header: pathlib.Path,
        *,
        with_library: bool,
    ) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path, pathlib.Path | None]:
        temporary = tempfile.TemporaryDirectory(prefix="coreguard-consumer-", dir=BUILD)
        root = pathlib.Path(temporary.name)
        include_dir = root / "include"
        include_dir.mkdir()
        shutil.copy2(header, include_dir / "coreguard.h")
        staged_source = root / source.name
        shutil.copy2(source, staged_source)
        staged_library = None
        if with_library:
            library_dir = root / "lib"
            library_dir.mkdir()
            staged_library = library_dir / "coreguard.lib"
            shutil.copy2(self.library, staged_library)
        return temporary, staged_source, staged_library

    def compile_consumer(
        self,
        source: pathlib.Path,
        header: pathlib.Path,
        *,
        cpp: bool = False,
        with_library: bool = True,
        defines: tuple[str, ...] = (),
    ) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path, subprocess.CompletedProcess[str]]:
        temporary, staged_source, staged_library = self.stage(
            source, header, with_library=with_library
        )
        root = pathlib.Path(temporary.name)
        output = root / "consumer.exe"
        language_flags = "/EHsc /std:c++17" if cpp else "/std:c11 /TC"
        define_flags = " ".join(f"/D{define}" for define in defines)
        command = (
            f'call "{VCVARS}" && '
            f'cl /nologo /W4 /WX /analyze /wd28301 /MD {language_flags} '
            f'{define_flags} /I"{root / "include"}" "{staged_source}" '
            f'/Fe:"{output}"'
        )
        if staged_library is not None:
            command += f' /link "{staged_library}"'
        completed = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        return temporary, output, completed

    @staticmethod
    def assert_compile_success(
        test_case: unittest.TestCase,
        completed: subprocess.CompletedProcess[str],
    ) -> None:
        test_case.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def assert_runs(self, executable: pathlib.Path) -> None:
        completed = subprocess.run(
            [str(executable)],
            cwd=executable.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_clean_room_c_consumers(self) -> None:
        for name in ("legacy_basic", "current_basic", "current_metrics"):
            with self.subTest(consumer=name):
                temporary, executable, completed = self.compile_consumer(
                    CONSUMERS / name / "main.c",
                    PUBLIC_HEADER,
                )
                try:
                    self.assert_compile_success(self, completed)
                    self.assert_runs(executable)
                finally:
                    temporary.cleanup()

    def test_parent_releases_child_handle_duplicates_while_run_is_active(self) -> None:
        temporary, executable, completed = self.compile_consumer(
            CONSUMERS / "handle_lifetime" / "main.c",
            PUBLIC_HEADER,
        )
        try:
            self.assert_compile_success(self, completed)
            self.assert_runs(executable)
        finally:
            temporary.cleanup()

    def test_cpp_and_header_self_containment(self) -> None:
        cases = (
            ("cpp_basic", "main.cpp", True, True),
            ("header_only_c", "main.c", False, False),
            ("header_only_cpp", "main.cpp", True, False),
        )
        for name, source_name, cpp, with_library in cases:
            with self.subTest(consumer=name):
                temporary, executable, completed = self.compile_consumer(
                    CONSUMERS / name / source_name,
                    PUBLIC_HEADER,
                    cpp=cpp,
                    with_library=with_library,
                )
                try:
                    self.assert_compile_success(self, completed)
                    self.assert_runs(executable)
                finally:
                    temporary.cleanup()

    def test_layout_manifest_and_packing_guard(self) -> None:
        temporary, executable, completed = self.compile_consumer(
            CONSUMERS / "layout_manifest" / "main.c",
            PUBLIC_HEADER,
            with_library=False,
        )
        try:
            self.assert_compile_success(self, completed)
            manifest = subprocess.run(
                [str(executable)],
                cwd=executable.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(manifest.returncode, 0, manifest.stderr)
            observed = dict(
                line.split("=", 1)
                for line in manifest.stdout.splitlines()
                if "=" in line
            )
            self.assertEqual(observed["pointer_bits"], "64")
            expected = {
                "sizeof(cg_resource_limits)": "32",
                "alignof(cg_resource_limits)": "8",
                "sizeof(cg_run_options)": "40",
                "offsetof(cg_run_options,resource_limits)": "32",
                "sizeof(cg_process_metrics)": "80",
                "sizeof(cg_job_metrics)": "104",
                "alignof(cg_job_metrics)": "8",
                "sizeof(cg_run_result)": "176",
                "offsetof(cg_run_result,duration_ms)": "32",
            }
            self.assertEqual(
                {key: observed[key] for key in expected},
                expected,
            )
        finally:
            temporary.cleanup()

        temporary, _, packed = self.compile_consumer(
            CONSUMERS / "layout_manifest" / "main.c",
            PUBLIC_HEADER,
            with_library=False,
            defines=("COREGUARD_PACK_2",),
        )
        try:
            self.assertNotEqual(packed.returncode, 0)
            self.assertIn("static assertion failed", packed.stdout + packed.stderr)
        finally:
            temporary.cleanup()

    def test_pre_resource_binary_links_but_is_not_runtime_compatible(self) -> None:
        temporary, executable, completed = self.compile_consumer(
            CONSUMERS / "compatibility_pre_resource" / "main.c",
            PRE_RESOURCE_HEADER,
        )
        try:
            self.assert_compile_success(self, completed)
            self.assertTrue(executable.exists())
        finally:
            temporary.cleanup()

        temporary, executable, completed = self.compile_consumer(
            CONSUMERS / "compatibility_pre_resource" / "layout.c",
            PRE_RESOURCE_HEADER,
            with_library=False,
        )
        try:
            self.assert_compile_success(self, completed)
            self.assert_runs(executable)
            completed = subprocess.run(
                [str(executable)],
                cwd=executable.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            observed = dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines()
                if "=" in line
            )
            self.assertEqual(observed["sizeof(cg_run_options)"], "32")
            self.assertEqual(observed["sizeof(cg_run_result)"], "160")
            self.assertEqual(observed["offsetof(cg_run_result,cleanup_ok)"], "8")
            self.assertEqual(observed["offsetof(cg_run_result,metrics)"], "80")
        finally:
            temporary.cleanup()

    def test_resource_limit_object_runs_with_current_static_library(self) -> None:
        temporary, executable, completed = self.compile_consumer(
            CONSUMERS / "compatibility_resource_limits" / "main.c",
            RESOURCE_LIMIT_HEADER,
        )
        try:
            self.assert_compile_success(self, completed)
            self.assert_runs(executable)
        finally:
            temporary.cleanup()

    def test_missing_public_symbol_fails_at_link(self) -> None:
        temporary, _, completed = self.compile_consumer(
            CONSUMERS / "negative_missing_symbol" / "main.c",
            PUBLIC_HEADER,
        )
        try:
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "cg_missing_public_symbol",
                completed.stdout + completed.stderr,
            )
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
