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

#ifdef COREGUARD_TEST_HOOKS
int cg_windows_run_with_job_metrics_query_hook(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    cg_query_job_information_fn query_job_information);
#endif

#endif
