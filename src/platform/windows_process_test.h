#ifndef COREGUARD_WINDOWS_PROCESS_TEST_H
#define COREGUARD_WINDOWS_PROCESS_TEST_H

#include <windows.h>

#include "coreguard.h"

typedef BOOL(WINAPI *cg_query_job_information_fn)(
    HANDLE job,
    JOBOBJECTINFOCLASS information_class,
    LPVOID information,
    DWORD information_length,
    LPDWORD returned_length);

typedef BOOL(WINAPI *cg_read_file_fn)(HANDLE file, LPVOID buffer,
                                      DWORD bytes_to_read,
                                      LPDWORD bytes_read,
                                      LPOVERLAPPED overlapped);
typedef BOOL(WINAPI *cg_assign_process_to_job_fn)(HANDLE job, HANDLE process);
typedef DWORD(WINAPI *cg_wait_single_fn)(HANDLE handle, DWORD timeout_ms);
typedef BOOL(WINAPI *cg_cancel_synchronous_io_fn)(HANDLE thread);

typedef struct cg_windows_test_hooks {
    cg_query_job_information_fn query_job_information;
    cg_read_file_fn read_file;
    cg_assign_process_to_job_fn assign_process_to_job;
    cg_wait_single_fn wait_capture_thread;
    cg_cancel_synchronous_io_fn cancel_synchronous_io;
    DWORD capture_cancel_grace_ms;
} cg_windows_test_hooks;

#ifdef COREGUARD_TEST_HOOKS
int cg_windows_run_with_job_metrics_query_hook(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    cg_query_job_information_fn query_job_information);
int cg_windows_run_with_test_hooks(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    const cg_windows_test_hooks *hooks);
LONG cg_windows_test_quarantined_capture_count(void);
LONG cg_windows_test_capture_slot_limit(void);
#endif

#endif
