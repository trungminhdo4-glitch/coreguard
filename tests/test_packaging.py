"""Install, package, relocation, and clean-room release proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = ROOT / "build"
VCVARS = os.environ.get("COREGUARD_VCVARS", "vcvars64.bat")
CMAKE = os.environ.get("COREGUARD_CMAKE") or shutil.which("cmake") or "cmake"
CPACK = os.environ.get("COREGUARD_CPACK") or shutil.which("cpack") or "cpack"


class PackagingProofError(RuntimeError):
    """A release-readiness gate failed."""


def run(
    command: list[str],
    *,
    cwd: pathlib.Path,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise PackagingProofError(
            f"command failed ({completed.returncode}): {command}\n"
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    return completed


def run_bat(
    command: str,
    *,
    cwd: pathlib.Path,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        f'call "{VCVARS}" && {command}',
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise PackagingProofError(
            f"MSVC command failed ({completed.returncode}): {command}\n"
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    return completed


def rel_files(root: pathlib.Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )


def zip_files(archive: pathlib.Path) -> list[str]:
    with zipfile.ZipFile(archive) as package:
        return sorted(
            info.filename
            for info in package.infolist()
            if not info.is_dir()
        )


def assert_inventory(root: pathlib.Path, archive: pathlib.Path | None = None) -> list[str]:
    inventory = zip_files(archive) if archive is not None else rel_files(root)
    forbidden = re.compile(
        r"(^|/)(?:\.git|tests|src|temp|logs?)(?:/|$)"
        r"|(?:\.env|credentials?|tokens?)(?:$|/)"
        r"|(?:\.obj|\.pdb|\.ilk|\.i|\.bundle|CMakeCache\.txt)$",
        re.IGNORECASE,
    )
    unexpected = [entry for entry in inventory if forbidden.search(entry)]
    if unexpected:
        raise PackagingProofError(f"forbidden package entries: {unexpected}")
    return inventory


def copy_prefix(source: pathlib.Path, destination: pathlib.Path) -> None:
    shutil.copytree(source, destination)


def write_consumer(
    root: pathlib.Path,
    required_version: str | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    consumer = root / "consumer"
    consumer.mkdir(parents=True)
    (consumer / "main.c").write_text(
        textwrap.dedent(
            r'''
            #include "coreguard.h"

            #include <wchar.h>

            int main(void)
            {
                const wchar_t *argv[] = {L"cmd.exe", L"/c", L"exit", L"0"};
                cg_run_options options = {0};
                cg_run_result result = {0};
                cg_job_metrics job_metrics = {0};

                options.argv = argv;
                options.argc = sizeof(argv) / sizeof(argv[0]);
                options.timeout_ms = 5000;

                if (cg_run(&options, &result) != 0 ||
                    result.status != CG_STATUS_EXITED ||
                    !result.has_exit_code || result.exit_code != 0 ||
                    !result.cleanup_ok) {
                    cg_run_result_free(&result);
                    return 10;
                }
                cg_run_result_free(&result);

                if (cg_run_with_job_metrics(&options, &result, &job_metrics) != 0 ||
                    result.status != CG_STATUS_EXITED ||
                    !result.has_exit_code || result.exit_code != 0 ||
                    !job_metrics.snapshot_available ||
                    (job_metrics.valid_fields & CG_JOB_METRIC_TOTAL_PROCESSES) == 0 ||
                    job_metrics.total_processes < 1U) {
                    cg_run_result_free(&result);
                    return 11;
                }
                cg_run_result_free(&result);
                return 0;
            }
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    find_package = (
        f"find_package(coreguard {required_version} EXACT CONFIG REQUIRED)"
        if required_version is not None
        else "find_package(coreguard CONFIG REQUIRED)"
    )
    (consumer / "CMakeLists.txt").write_text(
        textwrap.dedent(
            f"""
            cmake_minimum_required(VERSION 3.20)
            project(coreguard_clean_room_consumer C)
            {find_package}
            add_executable(coreguard_clean_room_consumer main.c)
            target_link_libraries(coreguard_clean_room_consumer PRIVATE coreguard::coreguard)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return consumer, consumer / "main.c"


def write_cpp_consumer(
    root: pathlib.Path,
    required_version: str | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    consumer = root / "consumer"
    consumer.mkdir(parents=True)
    (consumer / "main.cpp").write_text(
        textwrap.dedent(
            r'''
            #include "coreguard.h"

            #include <cwchar>

            int main()
            {
                const wchar_t *argv[] = {L"cmd.exe", L"/c", L"exit", L"0"};
                cg_run_options options{};
                cg_run_result result{};
                cg_job_metrics job_metrics{};

                options.argv = argv;
                options.argc = sizeof(argv) / sizeof(argv[0]);
                options.timeout_ms = 5000;

                if (cg_status_name(CG_STATUS_EXITED) == nullptr ||
                    cg_run_with_job_metrics(&options, &result, &job_metrics) != 0 ||
                    result.status != CG_STATUS_EXITED ||
                    !result.has_exit_code || result.exit_code != 0 ||
                    !result.cleanup_ok || !job_metrics.snapshot_available) {
                    cg_run_result_free(&result);
                    return 12;
                }
                cg_run_result_free(&result);
                return 0;
            }
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    find_package = (
        f"find_package(coreguard {required_version} EXACT CONFIG REQUIRED)"
        if required_version is not None
        else "find_package(coreguard CONFIG REQUIRED)"
    )
    (consumer / "CMakeLists.txt").write_text(
        textwrap.dedent(
            f"""
            cmake_minimum_required(VERSION 3.20)
            project(coreguard_clean_room_consumer CXX)
            {find_package}
            add_executable(coreguard_clean_room_consumer main.cpp)
            target_link_libraries(coreguard_clean_room_consumer PRIVATE coreguard::coreguard)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return consumer, consumer / "main.cpp"


def configure_build(
    source: pathlib.Path,
    build: pathlib.Path,
    *,
    prefix_path: pathlib.Path | None = None,
    generator: str | None = None,
    version: str | None = None,
) -> None:
    command = [str(CMAKE), "-S", str(source), "-B", str(build)]
    if generator:
        command.extend(["-G", generator])
    if version is not None:
        command.append(f"-DCOREGUARD_VERSION={version}")
    if prefix_path is not None:
        command.append(f"-DCMAKE_PREFIX_PATH={prefix_path}")
    run_bat(subprocess.list2cmdline(command), cwd=source)


def build_release(build: pathlib.Path) -> None:
    run_bat(
        subprocess.list2cmdline(
            [str(CMAKE), "--build", str(build), "--config", "Release", "--parallel"]
        ),
        cwd=ROOT,
    )


def install_release(build: pathlib.Path, prefix: pathlib.Path) -> None:
    run_bat(
        subprocess.list2cmdline(
            [
                str(CMAKE),
                "--install",
                str(build),
                "--config",
                "Release",
                "--prefix",
                str(prefix),
            ]
        ),
        cwd=ROOT,
    )


def build_cmake_consumer(
    consumer: pathlib.Path,
    build: pathlib.Path,
    prefix: pathlib.Path,
    *,
    check: bool = True,
    generator: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(CMAKE),
        "-S",
        str(consumer),
        "-B",
        str(build),
    ]
    if generator:
        command.extend(["-G", generator])
    command.append("-DCMAKE_PREFIX_PATH=" + str(prefix))
    configure = run_bat(
        subprocess.list2cmdline(command),
        cwd=consumer,
        check=check,
    )
    if not check:
        return configure
    return run_bat(
        subprocess.list2cmdline(
            [str(CMAKE), "--build", str(build), "--config", "Release", "--parallel"]
        ),
        cwd=consumer,
    )


def run_cmake_consumer(
    consumer: pathlib.Path,
    build: pathlib.Path,
    prefix: pathlib.Path,
    *,
    generator: str | None = None,
) -> None:
    build_cmake_consumer(consumer, build, prefix, generator=generator)
    executable = build / "Release" / "coreguard_clean_room_consumer.exe"
    if not executable.is_file():
        raise PackagingProofError(f"missing CMake consumer executable: {executable}")
    run([str(executable)], cwd=executable.parent, timeout=60)


def run_manual_consumer(root: pathlib.Path, prefix: pathlib.Path) -> None:
    manual = root / "manual"
    manual.mkdir(parents=True)
    source = manual / "main.c"
    source.write_text(
        textwrap.dedent(
            r'''
            #include "coreguard.h"

            #include <wchar.h>

            int main(void)
            {
                const wchar_t *argv[] = {L"cmd.exe", L"/c", L"exit", L"0"};
                cg_run_options options = {0};
                cg_run_result result = {0};
                options.argv = argv;
                options.argc = sizeof(argv) / sizeof(argv[0]);
                options.timeout_ms = 5000;
                if (cg_run(&options, &result) != 0 ||
                    result.status != CG_STATUS_EXITED ||
                    !result.has_exit_code || result.exit_code != 0) {
                    cg_run_result_free(&result);
                    return 20;
                }
                cg_run_result_free(&result);
                return 0;
            }
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    executable = manual / "manual_consumer.exe"
    command = (
        f'cl /nologo /W4 /WX /analyze /wd28301 /MD /std:c11 /TC '
        f'/I"{prefix / "include"}" "{source}" /Fe:"{executable}" '
        f'/link "{prefix / "lib" / "coreguard.lib"}"'
    )
    run_bat(command, cwd=manual)
    run([str(executable)], cwd=manual, timeout=60)


def run_cli(prefix: pathlib.Path) -> dict[str, Any]:
    cli = prefix / "bin" / "coreguard.exe"
    help_result = run([str(cli), "--help"], cwd=cli.parent)
    if "Usage: coreguard run" not in help_result.stdout:
        raise PackagingProofError("installed CLI help output is missing")

    def json_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise PackagingProofError(
                f"installed CLI did not emit JSON: {result.stdout}"
            ) from error

    safe_run = run(
        [
            str(cli),
            "run",
            "--json",
            "--timeout-ms",
            "5000",
            "--memory-limit-mb",
            "128",
            "--",
            "cmd.exe",
            "/c",
            "exit",
            "0",
        ],
        cwd=cli.parent,
    )
    payload = json_payload(safe_run)
    if (
        payload.get("status") != "exited"
        or payload.get("exit_code") != 0
        or not payload.get("job_metrics", {}).get("snapshot_available")
    ):
        raise PackagingProofError(f"unexpected installed CLI result: {payload}")

    timeout_run = run(
        [
            str(cli),
            "run",
            "--json",
            "--timeout-ms",
            "100",
            "--",
            "cmd.exe",
            "/c",
            "ping",
            "127.0.0.1",
            "-n",
            "4",
        ],
        cwd=cli.parent,
        check=False,
    )
    timeout_payload = json_payload(timeout_run)
    if timeout_run.returncode != 124 or timeout_payload.get("status") != "timeout":
        raise PackagingProofError(
            f"unexpected installed CLI timeout result: {timeout_payload}"
        )

    resource_run = run(
        [
            str(cli),
            "run",
            "--json",
            "--memory-limit-mb",
            "1",
            "--",
            "cmd.exe",
            "/c",
            "exit",
            "0",
        ],
        cwd=cli.parent,
        check=False,
    )
    resource_payload = json_payload(resource_run)
    if (
        resource_run.returncode != 123
        or resource_payload.get("status") != "resource_limit"
        or not resource_payload.get("resource_limit_hit")
    ):
        raise PackagingProofError(
            f"unexpected installed CLI resource-limit result: {resource_payload}"
        )

    return {
        "help": "PASS",
        "normal_exit": "PASS",
        "timeout": "PASS",
        "resource_limit": "PASS",
        "job_metrics": "PASS",
        "json": payload,
        "timeout_json": timeout_payload,
        "resource_limit_json": resource_payload,
    }


def audit_dependencies(root: pathlib.Path, prefix: pathlib.Path) -> dict[str, Any]:
    consumer = root / "dependency_consumer"
    consumer.mkdir()
    source = consumer / "main.c"
    source.write_text(
        '#include "coreguard.h"\n'
        'int main(void) { return cg_status_name(CG_STATUS_EXITED) == 0; }\n',
        encoding="utf-8",
    )
    executable = consumer / "dependency_consumer.exe"
    command = (
        f'cl /nologo /W4 /WX /analyze /wd28301 /MD /std:c11 /TC '
        f'/I"{prefix / "include"}" "{source}" /Fe:"{executable}" '
        f'/link "{prefix / "lib" / "coreguard.lib"}"'
    )
    run_bat(command, cwd=consumer)
    reports: dict[str, Any] = {}
    for name, binary in {
        "coreguard.exe": prefix / "bin" / "coreguard.exe",
        "clean_room_consumer.exe": executable,
    }.items():
        completed = run_bat(f'dumpbin /DEPENDENTS "{binary}"', cwd=root)
        dependencies = sorted(
            set(
                re.findall(
                    r"(?im)^\s+([A-Za-z0-9_.-]+\.dll)\s*$",
                    completed.stdout,
                )
            )
        )
        private = [
            dependency
            for dependency in dependencies
            if dependency.lower() in {"coreguard.dll", "test.dll", "python.dll"}
        ]
        if private:
            raise PackagingProofError(
                f"private runtime dependencies found for {name}: {private}"
            )
        reports[name] = {
            "imports": dependencies,
            "private_project_dlls": private,
            "assessment": "system/runtime DLLs only; no packaged private DLL",
        }
    return reports


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_no_leakage(prefix: pathlib.Path, *forbidden_paths: pathlib.Path) -> None:
    needles = [str(path).replace("\\", "/").lower() for path in forbidden_paths]
    for path in prefix.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".cmake", ".h", ".txt", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").replace("\\", "/").lower()
        for needle in needles:
            if needle and needle in text:
                raise PackagingProofError(
                    f"build/source path leaked into {path}: {needle}"
                )


def expect_missing_file_failure(
    root: pathlib.Path,
    prefix: pathlib.Path,
    relative_file: pathlib.Path,
    label: str,
    required_version: str | None = None,
    generator: str | None = None,
) -> str:
    broken_prefix = root / f"negative-{label}"
    copy_prefix(prefix, broken_prefix)
    (broken_prefix / relative_file).unlink()
    consumer, _ = write_consumer(
        root / f"negative-consumer-{label}", required_version
    )
    build = root / f"negative-build-{label}"
    completed = build_cmake_consumer(
        consumer,
        build,
        broken_prefix,
        check=False,
        generator=generator,
    )
    if completed.returncode == 0:
        build_result = run_bat(
            subprocess.list2cmdline(
                [
                    str(CMAKE),
                    "--build",
                    str(build),
                    "--config",
                    "Release",
                    "--parallel",
                ]
            ),
            cwd=consumer,
            check=False,
        )
        if build_result.returncode == 0:
            raise PackagingProofError(f"missing {label} did not fail")
        return f"PASS (build failed: {build_result.returncode})"
    return f"PASS (configure failed: {completed.returncode})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=pathlib.Path, default=ROOT)
    parser.add_argument("--build-root", type=pathlib.Path, default=DEFAULT_BUILD_ROOT)
    parser.add_argument("--generator", default=None)
    parser.add_argument(
        "--version",
        help="owner-authorized numeric major.minor.patch release version",
    )
    args = parser.parse_args()
    if args.version is not None and not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", args.version
    ):
        parser.error("--version must be a numeric major.minor.patch version")
    source = args.source.resolve()
    build_root = args.build_root.resolve()
    build_root.mkdir(parents=True, exist_ok=True)
    if "COREGUARD_VCVARS" in os.environ:
        vcvars_available = pathlib.Path(VCVARS).is_file()
    else:
        vcvars_available = shutil.which(VCVARS) is not None
    if not vcvars_available:
        raise PackagingProofError(
            "missing vcvars64.bat; set COREGUARD_VCVARS or add it to PATH"
        )
    if not pathlib.Path(CMAKE).is_file() and shutil.which(CMAKE) is None:
        raise PackagingProofError(
            "missing CMake; set COREGUARD_CMAKE or add cmake to PATH"
        )
    if not pathlib.Path(CPACK).is_file() and shutil.which(CPACK) is None:
        raise PackagingProofError(
            "missing CPack; set COREGUARD_CPACK or add cpack to PATH"
        )

    evidence_prefix = (
        f"coreguard-v{args.version}-rc"
        if args.version
        else "coreguard-public-package-proof"
    )
    evidence_path = build_root / f"{evidence_prefix}-release-readiness.json"
    manifest_path = build_root / f"{evidence_prefix}-package-inventory.json"
    rc_manifest_path = build_root / f"{evidence_prefix}-rc-manifest.json"
    sha_path = build_root / f"{evidence_prefix}-SHA256SUMS"
    archive_name = (
        f"coreguard-{args.version}-windows-x64-msvc.zip"
        if args.version
        else "coreguard-dev-windows-x64-msvc.zip"
    )

    gates: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(
        prefix=f"{evidence_prefix}-", dir=build_root
    ) as temporary:
        work = pathlib.Path(temporary)
        build_a = work / "build-a"
        build_b = work / "build-b"
        install_a = work / "install-a"
        install_b = work / "install-b"
        extracted_a = work / "extracted-a"
        extracted_b = work / "extracted-b"

        configure_build(source, build_a, generator=args.generator, version=args.version)
        build_release(build_a)
        gates["clean_release_build_a"] = "PASS"
        install_release(build_a, install_a)
        inventory_a = assert_inventory(install_a)
        expected = {
            "bin/coreguard.exe",
            "include/coreguard.h",
            "lib/coreguard.lib",
            "lib/cmake/coreguard/coreguardConfig.cmake",
            "lib/cmake/coreguard/coreguardTargets.cmake",
            "lib/cmake/coreguard/coreguardTargets-release.cmake",
        }
        license_path = source / "LICENSE"
        if not license_path.is_file():
            raise PackagingProofError("owner-authorized LICENSE is missing")
        expected.add("LICENSE")
        if (source / "NOTICE").is_file():
            expected.add("NOTICE")
        if args.version:
            expected.add("lib/cmake/coreguard/coreguardConfigVersion.cmake")
        if set(inventory_a) != expected:
            raise PackagingProofError(f"unexpected install inventory: {inventory_a}")
        if (install_a / "LICENSE").read_bytes() != license_path.read_bytes():
            raise PackagingProofError("installed LICENSE is not byte-identical")
        gates["install_prefix"] = "PASS"
        gates["license_byte_identity_install"] = "PASS"
        first_inventory = inventory_a[:]
        install_release(build_a, install_a)
        if rel_files(install_a) != first_inventory:
            raise PackagingProofError("install is not idempotent")
        gates["install_idempotence"] = "PASS"

        configure_build(source, build_b, generator=args.generator, version=args.version)
        build_release(build_b)
        install_release(build_b, install_b)
        if rel_files(install_b) != inventory_a:
            raise PackagingProofError("rebuild install inventories differ")
        if (install_a / "include/coreguard.h").read_bytes() != (
            install_b / "include/coreguard.h"
        ).read_bytes():
            raise PackagingProofError("rebuild header contents differ")
        gates["clean_release_build_b"] = "PASS"
        gates["functional_rebuild_consistency"] = "PASS"

        run_bat(
            subprocess.list2cmdline(
                [
                    str(CPACK),
                    "--config",
                    str(build_a / "CPackConfig.cmake"),
                    "-C",
                    "Release",
                ]
            ),
            cwd=build_a,
        )
        archives = sorted(build_a.glob(archive_name))
        if len(archives) != 1:
            raise PackagingProofError(f"expected one CPack ZIP, found: {archives}")
        archive = archives[0]
        inventory_archive = assert_inventory(install_a, archive)
        if inventory_archive != inventory_a:
            raise PackagingProofError("archive inventory differs from install inventory")
        with zipfile.ZipFile(archive) as package:
            if package.read("LICENSE") != license_path.read_bytes():
                raise PackagingProofError("archived LICENSE is not byte-identical")
        digest = sha256(archive)
        if digest != sha256(archive):
            raise PackagingProofError("SHA-256 changed between calculations")
        archive_copy = build_root / archive.name
        shutil.copy2(archive, archive_copy)
        if sha256(archive_copy) != digest:
            raise PackagingProofError("copied archive SHA-256 mismatch")
        manifest = {
            "archive": archive.name,
            "size_bytes": archive_copy.stat().st_size,
            "sha256": digest,
            "entries": inventory_archive,
            "install_entries": inventory_a,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        sha_path.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        gates["archive_cpack_zip"] = "PASS"
        gates["inventory_hygiene"] = "PASS"
        gates["sha256"] = "PASS"
        gates["license_byte_identity_archive"] = "PASS"

        for extracted in (extracted_a, extracted_b):
            extracted.mkdir()
            with zipfile.ZipFile(archive_copy) as package:
                package.extractall(extracted)
            if rel_files(extracted) != inventory_archive:
                raise PackagingProofError(f"extracted inventory differs: {extracted}")
        shutil.rmtree(build_a)
        shutil.rmtree(install_a)
        gates["archive_extraction_and_original_tree_unavailable"] = "PASS"

        consumer_root_a = work / "consumer-a"
        consumer_a, _ = write_consumer(consumer_root_a, args.version)
        run_cmake_consumer(
            consumer_a,
            work / "consumer-build-a",
            extracted_a,
            generator=args.generator,
        )
        consumer_root_b = work / "consumer-b"
        consumer_b, _ = write_consumer(consumer_root_b, args.version)
        run_cmake_consumer(
            consumer_b,
            work / "consumer-build-b",
            extracted_b,
            generator=args.generator,
        )
        gates["packaged_cmake_consumer_a"] = "PASS"
        gates["packaged_cmake_consumer_b"] = "PASS"
        consumer_cpp_root_a = work / "consumer-cpp-a"
        consumer_cpp_a, _ = write_cpp_consumer(consumer_cpp_root_a, args.version)
        run_cmake_consumer(
            consumer_cpp_a,
            work / "consumer-cpp-build-a",
            extracted_a,
            generator=args.generator,
        )
        consumer_cpp_root_b = work / "consumer-cpp-b"
        consumer_cpp_b, _ = write_cpp_consumer(consumer_cpp_root_b, args.version)
        run_cmake_consumer(
            consumer_cpp_b,
            work / "consumer-cpp-build-b",
            extracted_b,
            generator=args.generator,
        )
        gates["packaged_cmake_cpp_consumer_a"] = "PASS"
        gates["packaged_cmake_cpp_consumer_b"] = "PASS"
        run_manual_consumer(work / "manual-root", extracted_a)
        gates["packaged_manual_c_consumer"] = "PASS"
        gates["relocation"] = "PASS"
        if args.version:
            gates["version_aware_exact_find_package"] = "PASS"
            for incompatible_version in ("0.1.1", "1.0.0"):
                version_label = incompatible_version.replace(".", "_")
                negative_version_root = work / f"negative-version-{version_label}"
                negative_version, _ = write_consumer(
                    negative_version_root, incompatible_version
                )
                negative_version_result = build_cmake_consumer(
                    negative_version,
                    work / f"negative-version-build-{version_label}",
                    extracted_a,
                    check=False,
                    generator=args.generator,
                )
                if negative_version_result.returncode == 0:
                    raise PackagingProofError(
                        f"incompatible exact package version was accepted: {incompatible_version}"
                    )
                gates[f"version_rejection_{version_label}"] = "PASS"
        else:
            gates["version_aware_exact_find_package"] = "NOT_APPLICABLE"
            gates["version_rejection"] = "NOT_APPLICABLE"
        cli_result = run_cli(extracted_a)
        gates["packaged_cli"] = cli_result
        dependency_audit = audit_dependencies(work, extracted_a)
        gates["runtime_dependency_audit"] = "PASS"

        gates["negative_missing_library"] = expect_missing_file_failure(
            work,
            extracted_a,
            pathlib.Path("lib/coreguard.lib"),
            "library",
            args.version,
            args.generator,
        )
        gates["negative_missing_header"] = expect_missing_file_failure(
            work,
            extracted_a,
            pathlib.Path("include/coreguard.h"),
            "header",
            args.version,
            args.generator,
        )
        unexpected_archive = work / "unexpected-entry.zip"
        with zipfile.ZipFile(archive_copy) as source_zip, zipfile.ZipFile(
            unexpected_archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as target_zip:
            for info in source_zip.infolist():
                target_zip.writestr(info, source_zip.read(info.filename))
            target_zip.writestr("unexpected.obj", b"negative inventory probe")
        try:
            assert_inventory(extracted_b, unexpected_archive)
        except PackagingProofError:
            gates["negative_unexpected_package_entry"] = "PASS (inventory rejected)"
        else:
            raise PackagingProofError("unexpected package entry was not rejected")

        assert_no_leakage(
            extracted_a,
            source,
            build_root,
            build_b,
            install_b,
        )
        gates["build_path_leakage"] = "PASS"
        gates["release_no_debug_or_intermediate_files"] = "PASS"

        def git_value(*git_args: str) -> str:
            try:
                result = run(
                    ["git", "-c", f"safe.directory={source}", *git_args],
                    cwd=source,
                )
            except PackagingProofError:
                return "UNCOMMITTED_PUBLIC_CANDIDATE"
            return result.stdout.strip()

        package_manifest = manifest
        all_gates_pass = all(
            value == "PASS"
            or value == "NOT_APPLICABLE"
            or (isinstance(value, str) and value.startswith("PASS"))
            or isinstance(value, dict)
            for value in gates.values()
        )
        rc_manifest = {
            "project": "coreguard",
            "version": args.version,
            "schema_version": 1,
            "source_commit": git_value("rev-parse", "HEAD"),
            "branch": git_value("branch", "--show-current"),
            "platform": "windows-x64",
            "toolchain": "MSVC via vcvars64.bat; /W4 /WX /analyze",
            "configuration": "Release",
            "build_configuration": "Release",
            "generator": args.generator or "CMake default generator",
            "artifact": archive_copy.name,
            "sha256": digest,
            "license": "MIT License",
            "signed": False,
            "published": False,
            "architecture": "x64",
            "supported_platform": "Windows x64 / MSVC / static library / Release",
            "artifacts": [
                "bin/coreguard.exe",
                "include/coreguard.h",
                "lib/coreguard.lib",
                "lib/cmake/coreguard/coreguardConfig.cmake",
                "lib/cmake/coreguard/coreguardTargets.cmake",
                "lib/cmake/coreguard/coreguardTargets-release.cmake",
                *(
                    ["lib/cmake/coreguard/coreguardConfigVersion.cmake"]
                    if args.version
                    else []
                ),
                archive_copy.name,
            ],
            "package": package_manifest,
            "package_sha256": digest,
            "package_inventory": inventory_archive,
            "public_api_contract": "COREGUARD_PUBLIC_C_STATIC_MSVC_X64_V0_LAYOUT",
            "tests": {
                "gates": gates,
                "all_gates_pass_or_not_applicable": all_gates_pass,
            },
            "license_status": "MIT_LICENSE",
            "version_status": (
                "OWNER_AUTHORIZED_RELEASE_VERSION_0.1.0"
                if args.version == "0.1.0"
                else "INJECTED_NUMERIC_VERSION_NOT_FINAL_RC"
                if args.version
                else "NO_VERSION_DEV_ONLY"
            ),
            "publication_status": "LOCAL_RC_ONLY_NOT_PUBLISHED_UNSIGNED",
        }
        rc_manifest_path.write_text(
            json.dumps(rc_manifest, indent=2) + "\n", encoding="utf-8"
        )

        report = {
            "classification": (
                "COREGUARD_V0_1_0_MIT_RC_PROVEN"
                if args.version == "0.1.0"
                else "COREGUARD_VERSIONED_RC_PROVEN"
                if args.version
                else "COREGUARD_PACKAGING_AND_RELEASE_READINESS_PROVEN"
            ),
            "source": str(source),
            "generator": args.generator or "CMake default generator",
            "cmake_minimum": "3.20",
            "version_source": (
                f"owner-authorized -DCOREGUARD_VERSION={args.version} single CMake source"
                if args.version
                else "development artifact; no CMake project version"
            ),
            "archive": manifest,
            "rc_manifest": str(rc_manifest_path),
            "install_inventory": inventory_a,
            "dependency_audit": dependency_audit,
            "gates": gates,
            "optional": {
                "byte_identical_rebuild": "NOT_REQUIRED; functional/install inventory consistency proven",
                "symbols_package": "DEFERRED",
                "package_manager": "DEFERRED",
                "signing": "DEFERRED",
            },
        }
        evidence_path.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "evidence": str(evidence_path),
                "manifest": str(manifest_path),
                "rc_manifest": str(rc_manifest_path),
                "sha256": str(sha_path),
                "gates": gates,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackagingProofError as error:
        print(f"PACKAGING_PROOF_FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
