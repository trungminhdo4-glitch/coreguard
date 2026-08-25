#include <windows.h>

#include <process.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>

#include "coreguard.h"

typedef struct run_context {
    cg_run_options options;
    cg_run_result result;
    int api_result;
} run_context;

static unsigned __stdcall run_coreguard(void *opaque)
{
    run_context *context = (run_context *)opaque;

    context->api_result = cg_run(&context->options, &context->result);
    return 0U;
}

static int child_main(const wchar_t *ready_name, const wchar_t *release_name)
{
    const DWORD standard_ids[] = {
        STD_INPUT_HANDLE,
        STD_OUTPUT_HANDLE,
        STD_ERROR_HANDLE,
    };
    HANDLE ready = NULL;
    HANDLE release = NULL;
    size_t index;
    int status = 10;

    ready = OpenEventW(EVENT_MODIFY_STATE, FALSE, ready_name);
    release = OpenEventW(SYNCHRONIZE, FALSE, release_name);
    if (ready == NULL || release == NULL) {
        goto cleanup;
    }
    for (index = 0; index < sizeof(standard_ids) / sizeof(standard_ids[0]);
         ++index) {
        HANDLE handle = GetStdHandle(standard_ids[index]);
        if (handle == NULL || handle == INVALID_HANDLE_VALUE ||
            !SetStdHandle(standard_ids[index], NULL) || !CloseHandle(handle)) {
            status = 11;
            goto cleanup;
        }
    }
    if (!SetEvent(ready)) {
        status = 12;
        goto cleanup;
    }
    if (WaitForSingleObject(release, 10000U) != WAIT_OBJECT_0) {
        status = 13;
        goto cleanup;
    }
    status = 0;

cleanup:
    if (release != NULL) {
        CloseHandle(release);
    }
    if (ready != NULL) {
        CloseHandle(ready);
    }
    return status;
}

static int parent_main(void)
{
    const DWORD standard_ids[] = {
        STD_INPUT_HANDLE,
        STD_OUTPUT_HANDLE,
        STD_ERROR_HANDLE,
    };
    const wchar_t *standard_suffixes[] = {L"stdin", L"stdout", L"stderr"};
    wchar_t executable[MAX_PATH];
    wchar_t ready_name[128];
    wchar_t release_name[128];
    wchar_t standard_names[3][128];
    const wchar_t *child_argv[4];
    HANDLE original_standard[3];
    HANDLE standard_events[3] = {NULL, NULL, NULL};
    HANDLE ready = NULL;
    HANDLE release = NULL;
    HANDLE worker = NULL;
    run_context context;
    const wchar_t *failure = L"unknown failure";
    DWORD process_id = GetCurrentProcessId();
    DWORD nonce = GetTickCount();
    DWORD wait_result;
    size_t index;
    int standards_installed = 0;
    int worker_finished = 0;
    int status = 1;

    ZeroMemory(&context, sizeof(context));
    context.api_result = -1;
    if (GetModuleFileNameW(NULL, executable,
                           sizeof(executable) / sizeof(executable[0])) == 0) {
        failure = L"GetModuleFileNameW failed";
        goto cleanup;
    }
    if (swprintf_s(ready_name, sizeof(ready_name) / sizeof(ready_name[0]),
                   L"Local\\CoreguardHandleLifetime-%lu-%lu-ready",
                   (unsigned long)process_id, (unsigned long)nonce) < 0 ||
        swprintf_s(release_name,
                   sizeof(release_name) / sizeof(release_name[0]),
                   L"Local\\CoreguardHandleLifetime-%lu-%lu-release",
                   (unsigned long)process_id, (unsigned long)nonce) < 0) {
        failure = L"event name formatting failed";
        goto cleanup;
    }
    for (index = 0; index < 3U; ++index) {
        if (swprintf_s(standard_names[index],
                       sizeof(standard_names[index]) /
                           sizeof(standard_names[index][0]),
                       L"Local\\CoreguardHandleLifetime-%lu-%lu-%ls",
                       (unsigned long)process_id, (unsigned long)nonce,
                       standard_suffixes[index]) < 0) {
            failure = L"standard event name formatting failed";
            goto cleanup;
        }
        standard_events[index] =
            CreateEventW(NULL, TRUE, FALSE, standard_names[index]);
        if (standard_events[index] == NULL) {
            failure = L"standard event creation failed";
            goto cleanup;
        }
        original_standard[index] = GetStdHandle(standard_ids[index]);
    }
    ready = CreateEventW(NULL, TRUE, FALSE, ready_name);
    release = CreateEventW(NULL, TRUE, FALSE, release_name);
    if (ready == NULL || release == NULL) {
        failure = L"coordination event creation failed";
        goto cleanup;
    }

    standards_installed = 1;
    for (index = 0; index < 3U; ++index) {
        if (!SetStdHandle(standard_ids[index], standard_events[index])) {
            failure = L"standard handle installation failed";
            goto cleanup;
        }
    }

    child_argv[0] = executable;
    child_argv[1] = L"--child";
    child_argv[2] = ready_name;
    child_argv[3] = release_name;
    context.options.argv = child_argv;
    context.options.argc = sizeof(child_argv) / sizeof(child_argv[0]);
    context.options.timeout_ms = 10000U;
    context.options.capture_output = 0;
    worker = (HANDLE)(uintptr_t)_beginthreadex(
        NULL, 0U, run_coreguard, &context, 0U, NULL);
    if (worker == NULL) {
        failure = L"worker thread creation failed";
        goto cleanup;
    }
    if (WaitForSingleObject(ready, 5000U) != WAIT_OBJECT_0) {
        failure = L"child did not close its inherited standard handles";
        goto cleanup;
    }

    for (index = 0; index < 3U; ++index) {
        if (!SetStdHandle(standard_ids[index], original_standard[index])) {
            failure = L"standard handle restoration failed";
            goto cleanup;
        }
    }
    standards_installed = 0;
    for (index = 0; index < 3U; ++index) {
        CloseHandle(standard_events[index]);
        standard_events[index] = NULL;
    }

    if (WaitForSingleObject(worker, 0U) != WAIT_TIMEOUT) {
        failure = L"cg_run was not active during the lifetime probe";
        goto cleanup;
    }
    for (index = 0; index < 3U; ++index) {
        HANDLE probe;
        SetLastError(ERROR_SUCCESS);
        probe = OpenEventW(SYNCHRONIZE, FALSE, standard_names[index]);
        if (probe != NULL) {
            CloseHandle(probe);
            failure = L"parent retained an inheritable child handle";
            goto cleanup;
        }
        if (GetLastError() != ERROR_FILE_NOT_FOUND) {
            failure = L"named standard handle lifetime probe failed";
            goto cleanup;
        }
    }

    if (!SetEvent(release)) {
        failure = L"child release failed";
        goto cleanup;
    }
    wait_result = WaitForSingleObject(worker, 10000U);
    if (wait_result != WAIT_OBJECT_0) {
        failure = L"cg_run worker did not finish";
        goto cleanup;
    }
    worker_finished = 1;
    if (context.api_result != 0 || context.result.status != CG_STATUS_EXITED ||
        !context.result.has_exit_code || context.result.exit_code != 0U ||
        !context.result.cleanup_ok) {
        failure = L"cg_run result was not a clean child exit";
        goto cleanup;
    }
    status = 0;

cleanup:
    if (standards_installed) {
        for (index = 0; index < 3U; ++index) {
            SetStdHandle(standard_ids[index], original_standard[index]);
        }
    }
    for (index = 0; index < 3U; ++index) {
        if (standard_events[index] != NULL) {
            CloseHandle(standard_events[index]);
        }
    }
    if (release != NULL) {
        SetEvent(release);
    }
    if (worker != NULL) {
        if (!worker_finished) {
            worker_finished =
                WaitForSingleObject(worker, 15000U) == WAIT_OBJECT_0;
        }
        CloseHandle(worker);
    }
    if (release != NULL) {
        CloseHandle(release);
    }
    if (ready != NULL) {
        CloseHandle(ready);
    }
    if (worker_finished) {
        cg_run_result_free(&context.result);
    }
    if (status != 0) {
        fwprintf(stderr, L"%ls\n", failure);
    }
    return status;
}

int wmain(int argc, wchar_t **argv)
{
    if (argc == 4 && wcscmp(argv[1], L"--child") == 0) {
        return child_main(argv[2], argv[3]);
    }
    return parent_main();
}
