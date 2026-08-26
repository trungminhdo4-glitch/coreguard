"""Portable source-order checks for the Windows bounded capture lifecycle."""

from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WINDOWS_PROCESS = ROOT / "src" / "platform" / "windows_process.c"
NATIVE_CONSUMER = ROOT / "tests" / "consumers" / "bounded_output" / "main.c"
FAILURE_HARNESS = ROOT / "tests" / "native_capture_failures.c"


class BoundedOutputSourceTests(unittest.TestCase):
    def test_capture_uses_only_pipes_and_bounded_reader_buffers(self) -> None:
        source = WINDOWS_PROCESS.read_text(encoding="utf-8")

        for obsolete in (
            "GetTempPathW",
            "GetTempFileNameW",
            "FILE_ATTRIBUTE_TEMPORARY",
            "GetFileSizeEx",
        ):
            self.assertNotIn(obsolete, source)
        self.assertIn("CreatePipe(&capture->read_handle", source)
        self.assertIn("CreateThread(NULL, 0, cg_capture_reader", source)
        self.assertIn("? (size_t)CG_OUTPUT_LIMIT - capture->size", source)
        self.assertIn("InterlockedExchange(&capture->truncated, 1)", source)
        self.assertIn("CancelSynchronousIo", source)
        self.assertNotIn("TerminateThread", source)

    def test_spawn_and_cleanup_order_is_explicit(self) -> None:
        source = WINDOWS_PROCESS.read_text(encoding="utf-8")
        run = source.split("static int cg_windows_run_internal", 1)[1]

        allocate = run.index("cg_capture_create(&stdout_capture")
        reserve = run.index("cg_capture_reserve_slots(stdout_capture")
        close_source = run.index("cg_capture_close_write(stdout_capture")
        spawn = run.index("CreateProcessW(")
        assign = run.index("hooks->assign_process_to_job(job, process)")
        start_reader = run.index("cg_capture_start(stdout_capture")
        resume = run.index("ResumeThread(thread)")
        metrics = run.index("cg_collect_job_metrics(job")
        close_job = run.index("CloseHandle(job)")
        join_reader = run.index("cg_capture_join(stdout_capture")

        self.assertLess(allocate, close_source)
        self.assertLess(allocate, reserve)
        self.assertLess(reserve, spawn)
        self.assertLess(close_source, spawn)
        self.assertLess(spawn, assign)
        self.assertLess(assign, start_reader)
        self.assertLess(start_reader, resume)
        self.assertLess(metrics, close_job)
        self.assertLess(close_job, join_reader)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", run)

    def test_cancellation_and_failure_paths_are_bounded_and_guarded(self) -> None:
        source = WINDOWS_PROCESS.read_text(encoding="utf-8")
        join = source.split("static int cg_capture_join", 1)[1].split(
            "static int cg_capture_take", 1
        )[0]
        take = source.split("static int cg_capture_take", 1)[1].split(
            "static int cg_capture_close", 1
        )[0]
        close = source.split("static int cg_capture_close", 1)[1].split(
            "static void cg_normalize_newlines", 1
        )[0]

        self.assertIn("CG_CAPTURE_DRAIN_GRACE_MS", join)
        self.assertIn("CG_CAPTURE_CANCEL_POLL_MS", join)
        self.assertIn("deadline = cg_now_ms()", join)
        self.assertIn("SetEvent(capture->stop_event)", join)
        self.assertIn("cancel_io(capture->thread)", join)
        self.assertLess(
            join.index("SetEvent(capture->stop_event)"),
            join.index("if (cg_now_ms() >= deadline)"),
        )
        self.assertNotIn("INFINITE", join)
        self.assertIn("!capture->thread_joined", take)
        self.assertIn("!capture->thread_joined", close)
        self.assertIn("cg_capture_reap_quarantined", source)
        self.assertIn("CG_CAPTURE_QUARANTINE_LIMIT", source)
        self.assertIn("reaped < CG_CAPTURE_REAP_LIMIT", source)
        self.assertIn("ERROR_NOT_ENOUGH_QUOTA", source)
        self.assertIn("cg_terminate_unassigned_process", source)
        self.assertIn("DWORD boundary_wait = WaitForSingleObject(process, 0)", source)

    def test_native_proof_checks_both_prefixes_timeout_and_temp_directory(self) -> None:
        consumer = NATIVE_CONSUMER.read_text(encoding="utf-8")
        integration = (ROOT / "tests" / "test_public_c_consumer.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("prefix_is_exact(result.stdout_utf8", consumer)
        self.assertIn("prefix_is_exact(result.stderr_utf8", consumer)
        self.assertIn("CG_STATUS_TIMEOUT", consumer)
        self.assertIn('L"--exact"', consumer)
        self.assertIn('L"--retained"', consumer)
        self.assertIn("signal_output_complete", consumer)
        self.assertIn('environment["TEMP"] = str(capture_temp)', integration)
        self.assertIn('environment["TMP"] = str(capture_temp)', integration)
        self.assertIn("max_temp_file_size", integration)
        self.assertIn("while process.poll() is None", integration)
        self.assertIn('list(capture_temp.rglob("*"))', integration)

    def test_private_failure_harness_covers_prefix_assignment_and_join(self) -> None:
        harness = FAILURE_HARNESS.read_text(encoding="utf-8")

        self.assertIn("fail_after_prefix", harness)
        self.assertIn("fail_assignment", harness)
        self.assertIn("force_initial_capture_timeout", harness)
        self.assertIn("count_cancellation", harness)
        self.assertIn("run_failed_join_quarantine", harness)
        self.assertIn("run_repeated_cancellation", harness)
        self.assertIn("delay_cancellation", harness)
        self.assertIn("run_quarantine_cap", harness)
        self.assertIn("cg_windows_test_quarantined_capture_count", harness)
        self.assertIn("result.process_id == 0U", harness)
        self.assertIn("cg_windows_run_with_test_hooks", harness)


if __name__ == "__main__":
    unittest.main(verbosity=2)
