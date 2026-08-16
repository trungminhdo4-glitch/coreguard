#include <windows.h>

#include <stdio.h>

#include "coreguard.h"
#include "platform/windows_process_test.h"

static BOOL WINAPI fail_job_query(HANDLE job,
                                  JOBOBJECTINFOCLASS information_class,
                                  LPVOID information,
                                  DWORD information_length,
                                  LPDWORD returned_length)
{
    (void)job;
    (void)information_class;
    (void)information;
    (void)information_length;
    (void)returned_length;
    SetLastError(ERROR_ACCESS_DENIED);
    return FALSE;
}

int wmain(void)
{
    const wchar_t *argv[] = {L"cmd.exe", L"/d", L"/c", L"exit 7"};
    cg_run_options options;
    cg_run_result result;
    cg_job_metrics job_metrics;

    ZeroMemory(&options, sizeof(options));
    options.argv = argv;
    options.argc = sizeof(argv) / sizeof(argv[0]);
    options.timeout_ms = 5000U;
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&job_metrics, sizeof(job_metrics));

    if (cg_windows_run_with_job_metrics_query_hook(
            &options, &result, &job_metrics, fail_job_query) != 0) {
        fprintf(stderr, "native query hook returned an API error\n");
        return 1;
    }
    if (result.status != CG_STATUS_EXITED || !result.has_exit_code ||
        result.exit_code != 7U || !result.cleanup_ok) {
        fprintf(stderr, "primary result did not survive metrics failure\n");
        cg_run_result_free(&result);
        return 1;
    }
    if (job_metrics.snapshot_available != 0U ||
        job_metrics.valid_fields != 0U ||
        job_metrics.query_error != ERROR_ACCESS_DENIED) {
        fprintf(stderr, "metrics failure was not explicit\n");
        cg_run_result_free(&result);
        return 1;
    }
    cg_run_result_free(&result);
    return 0;
}
