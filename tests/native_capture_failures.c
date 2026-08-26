#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "coreguard.h"
#include "platform/windows_process_test.h"

static PVOID volatile selected_read_handle;
static volatile LONG fail_selected_read;
static volatile LONG capture_wait_calls;
static volatile LONG cancel_calls;
static volatile LONG delayed_cancel_calls;

static BOOL WINAPI fail_after_prefix(HANDLE file, LPVOID buffer,
                                     DWORD bytes_to_read,
                                     LPDWORD bytes_read,
                                     LPOVERLAPPED overlapped)
{
    HANDLE selected = (HANDLE)InterlockedCompareExchangePointer(
        &selected_read_handle, NULL, NULL);

    if (buffer == NULL || bytes_read == NULL) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    if (selected == file &&
        InterlockedCompareExchange(&fail_selected_read, 0, 1) == 1) {
        *bytes_read = 0U;
        SetLastError(ERROR_READ_FAULT);
        return FALSE;
    }
    if (!ReadFile(file, buffer, bytes_to_read, bytes_read, overlapped)) {
        return FALSE;
    }
    if (*bytes_read > 0U) {
        selected = (HANDLE)InterlockedCompareExchangePointer(
            &selected_read_handle, file, NULL);
        if (selected == NULL || selected == file) {
            (void)InterlockedExchange(&fail_selected_read, 1);
        }
    }
    return TRUE;
}

static BOOL WINAPI fail_assignment(HANDLE job, HANDLE process)
{
    (void)job;
    (void)process;
    SetLastError(ERROR_ACCESS_DENIED);
    return FALSE;
}

static DWORD WINAPI force_initial_capture_timeout(HANDLE handle,
                                                   DWORD timeout_ms)
{
    (void)InterlockedIncrement(&capture_wait_calls);
    if (timeout_ms == 100U) {
        return WAIT_TIMEOUT;
    }
    return WaitForSingleObject(handle, timeout_ms);
}

static BOOL WINAPI count_cancellation(HANDLE thread)
{
    (void)InterlockedIncrement(&cancel_calls);
    return CancelSynchronousIo(thread);
}

static BOOL WINAPI delay_cancellation(HANDLE thread)
{
    LONG call = InterlockedIncrement(&delayed_cancel_calls);

    if (call <= 3L) {
        SetLastError(ERROR_NOT_FOUND);
        return FALSE;
    }
    return CancelSynchronousIo(thread);
}

static BOOL WINAPI never_cancel(HANDLE thread)
{
    (void)thread;
    SetLastError(ERROR_NOT_FOUND);
    return FALSE;
}

static DWORD WINAPI fail_capture_join(HANDLE handle, DWORD timeout_ms)
{
    (void)handle;
    (void)timeout_ms;
    SetLastError(WAIT_TIMEOUT);
    return WAIT_TIMEOUT;
}

static int write_payload(DWORD size)
{
    char data[4096];
    DWORD remaining = size;

    memset(data, 'x', sizeof(data));
    while (remaining > 0U) {
        DWORD count = remaining > (DWORD)sizeof(data)
                          ? (DWORD)sizeof(data)
                          : remaining;
        DWORD written = 0U;
        if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), data, count, &written,
                       NULL) || written == 0U) {
            return 10;
        }
        if (written > remaining) {
            return 11;
        }
        remaining -= written;
    }
    return 0;
}

static int retain_writers(DWORD process_id)
{
    HANDLE process;
    HANDLE retained_stdout = NULL;
    HANDLE retained_stderr = NULL;
    int result;

    process = OpenProcess(PROCESS_DUP_HANDLE, FALSE, process_id);
    if (process == NULL) {
        return 12;
    }
    if (!DuplicateHandle(GetCurrentProcess(),
                         GetStdHandle(STD_OUTPUT_HANDLE), process,
                         &retained_stdout, 0, FALSE, DUPLICATE_SAME_ACCESS) ||
        !DuplicateHandle(GetCurrentProcess(),
                         GetStdHandle(STD_ERROR_HANDLE), process,
                         &retained_stderr, 0, FALSE, DUPLICATE_SAME_ACCESS) ||
        retained_stdout == NULL || retained_stderr == NULL) {
        CloseHandle(process);
        return 13;
    }
    /* These numeric handle values belong to the target process. */
    result = write_payload(1024U);
    CloseHandle(process);
    return result;
}

static int prefix_is_x(const cg_run_result *result)
{
    size_t index;

    if (result == NULL || result->stdout_utf8 == NULL ||
        result->stdout_size == 0U ||
        result->stdout_size > 8192U) {
        return 0;
    }
    for (index = 0U; index < result->stdout_size; index++) {
        if (result->stdout_utf8[index] != 'x') {
            return 0;
        }
    }
    return 1;
}

static int run_read_failure(const wchar_t *module)
{
    const wchar_t *arguments[] = {module, L"--payload-large"};
    cg_run_options options;
    cg_run_result result;
    cg_windows_test_hooks hooks;
    int valid;

    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&hooks, sizeof(hooks));
    options.argv = arguments;
    options.argc = _countof(arguments);
    options.timeout_ms = 5000U;
    options.capture_output = 1;
    hooks.read_file = fail_after_prefix;
    if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) != 0) {
        return 0;
    }
    valid = result.status == CG_STATUS_INTERNAL_ERROR &&
            result.win32_error == ERROR_READ_FAULT && prefix_is_x(&result);
    cg_run_result_free(&result);
    return valid;
}

static int run_assignment_failure(const wchar_t *module)
{
    const wchar_t *arguments[] = {module, L"--hold"};
    cg_run_options options;
    cg_run_result result;
    cg_windows_test_hooks hooks;
    HANDLE process;
    int valid;

    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&hooks, sizeof(hooks));
    options.argv = arguments;
    options.argc = _countof(arguments);
    options.timeout_ms = 5000U;
    hooks.assign_process_to_job = fail_assignment;
    if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) != 0) {
        return 0;
    }
    valid = result.status == CG_STATUS_CONTAINMENT_FAILED &&
            result.cleanup_ok && result.win32_error == ERROR_ACCESS_DENIED;
    process = OpenProcess(SYNCHRONIZE, FALSE, result.process_id);
    if (process != NULL) {
        valid = valid && WaitForSingleObject(process, 0) == WAIT_OBJECT_0;
        CloseHandle(process);
    }
    cg_run_result_free(&result);
    return valid;
}

static int run_join_injection(const wchar_t *module)
{
    const wchar_t *arguments[] = {module, L"--payload-small"};
    cg_run_options options;
    cg_run_result result;
    cg_windows_test_hooks hooks;
    int valid;

    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&hooks, sizeof(hooks));
    options.argv = arguments;
    options.argc = _countof(arguments);
    options.timeout_ms = 5000U;
    options.capture_output = 1;
    hooks.wait_capture_thread = force_initial_capture_timeout;
    hooks.cancel_synchronous_io = count_cancellation;
    if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) != 0) {
        return 0;
    }
    valid = result.status == CG_STATUS_EXITED && result.has_exit_code &&
            result.exit_code == 0U && result.cleanup_ok &&
            result.stdout_size == 1024U && prefix_is_x(&result) &&
            InterlockedCompareExchange(&capture_wait_calls, 0, 0) >= 4 &&
            InterlockedCompareExchange(&cancel_calls, 0, 0) >= 2;
    cg_run_result_free(&result);
    return valid;
}

static int run_failed_join_quarantine(const wchar_t *module)
{
    const wchar_t *arguments[] = {module, L"--payload-small"};
    cg_run_options options;
    cg_run_result result;
    cg_windows_test_hooks hooks;
    ULONGLONG started;
    int valid;

    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&hooks, sizeof(hooks));
    options.argv = arguments;
    options.argc = _countof(arguments);
    options.timeout_ms = 5000U;
    options.capture_output = 1;
    hooks.wait_capture_thread = fail_capture_join;
    hooks.capture_cancel_grace_ms = 25U;
    started = GetTickCount64();
    if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) != 0) {
        return 0;
    }
    valid = result.status == CG_STATUS_INTERNAL_ERROR && !result.cleanup_ok &&
            result.stdout_utf8 == NULL && result.stderr_utf8 == NULL &&
            GetTickCount64() - started < 2000U;
    cg_run_result_free(&result);
    return valid;
}

static int run_repeated_cancellation(const wchar_t *module)
{
    wchar_t process_id[32];
    const wchar_t *arguments[] = {module, L"--payload-retained", process_id};
    cg_run_options options;
    cg_run_result result;
    cg_windows_test_hooks hooks;
    ULONGLONG started;
    int valid;

    if (swprintf_s(process_id, _countof(process_id), L"%lu",
                   GetCurrentProcessId()) < 0) {
        return 0;
    }
    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    ZeroMemory(&hooks, sizeof(hooks));
    (void)InterlockedExchange(&delayed_cancel_calls, 0);
    options.argv = arguments;
    options.argc = _countof(arguments);
    options.timeout_ms = 5000U;
    options.capture_output = 1;
    hooks.cancel_synchronous_io = delay_cancellation;
    started = GetTickCount64();
    if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) != 0) {
        return 0;
    }
    valid = result.status == CG_STATUS_EXITED && result.has_exit_code &&
            result.exit_code == 0U && result.cleanup_ok &&
            result.stdout_size == 1024U && prefix_is_x(&result) &&
            InterlockedCompareExchange(&delayed_cancel_calls, 0, 0) >= 5L &&
            GetTickCount64() - started < 2000U;
    cg_run_result_free(&result);
    return valid;
}

static int run_quarantine_cap(const wchar_t *module)
{
    wchar_t process_id[32];
    const wchar_t *retained_arguments[] = {
        module, L"--payload-retained", process_id};
    const wchar_t *rejected_arguments[] = {module, L"--payload-small"};
    cg_run_options options;
    cg_windows_test_hooks hooks;
    LONG limit = cg_windows_test_capture_slot_limit();
    LONG run;

    if (limit <= 0L || (limit % 2L) != 0L ||
        cg_windows_test_quarantined_capture_count() != 0L ||
        swprintf_s(process_id, _countof(process_id), L"%lu",
                   GetCurrentProcessId()) < 0) {
        return 0;
    }
    ZeroMemory(&hooks, sizeof(hooks));
    hooks.cancel_synchronous_io = never_cancel;
    hooks.capture_cancel_grace_ms = 25U;
    for (run = 0L; run < limit / 2L; run++) {
        cg_run_result result;
        ULONGLONG started;
        int valid;

        ZeroMemory(&options, sizeof(options));
        ZeroMemory(&result, sizeof(result));
        options.argv = retained_arguments;
        options.argc = _countof(retained_arguments);
        options.timeout_ms = 5000U;
        options.capture_output = 1;
        started = GetTickCount64();
        if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) !=
            0) {
            return 0;
        }
        valid = result.status == CG_STATUS_INTERNAL_ERROR &&
                !result.cleanup_ok && result.stdout_utf8 == NULL &&
                result.stderr_utf8 == NULL &&
                GetTickCount64() - started < 1500U;
        cg_run_result_free(&result);
        if (!valid) {
            return 0;
        }
    }
    if (cg_windows_test_quarantined_capture_count() != limit) {
        return 0;
    }
    {
        cg_run_result result;
        ULONGLONG started;
        int valid;

        ZeroMemory(&options, sizeof(options));
        ZeroMemory(&result, sizeof(result));
        options.argv = rejected_arguments;
        options.argc = _countof(rejected_arguments);
        options.timeout_ms = 5000U;
        options.capture_output = 1;
        started = GetTickCount64();
        if (cg_windows_run_with_test_hooks(&options, &result, NULL, &hooks) !=
            0) {
            return 0;
        }
        valid = result.status == CG_STATUS_INTERNAL_ERROR &&
                result.win32_error == ERROR_NOT_ENOUGH_QUOTA &&
                result.process_id == 0U && result.cleanup_ok &&
                GetTickCount64() - started < 500U;
        cg_run_result_free(&result);
        return valid;
    }
}

int wmain(int argc, wchar_t **argv)
{
    wchar_t module[MAX_PATH];
    DWORD length;

    if (argc == 2 && wcscmp(argv[1], L"--payload-large") == 0) {
        return write_payload(16384U);
    }
    if (argc == 2 && wcscmp(argv[1], L"--payload-small") == 0) {
        return write_payload(1024U);
    }
    if (argc == 2 && wcscmp(argv[1], L"--hold") == 0) {
        Sleep(10000U);
        return 0;
    }
    if (argc == 3 && wcscmp(argv[1], L"--payload-retained") == 0) {
        return retain_writers(wcstoul(argv[2], NULL, 10));
    }
    length = GetModuleFileNameW(NULL, module, (DWORD)_countof(module));
    if (length == 0U || length >= (DWORD)_countof(module)) {
        return 1;
    }
    if (!run_read_failure(module)) {
        fprintf(stderr, "late read failure did not retain its prefix\n");
        return 2;
    }
    if (!run_assignment_failure(module)) {
        fprintf(stderr, "assignment failure cleanup was not confirmed\n");
        return 3;
    }
    if (!run_join_injection(module)) {
        fprintf(stderr, "capture cancellation hooks were not exercised\n");
        return 4;
    }
    if (!run_failed_join_quarantine(module)) {
        fprintf(stderr, "failed capture join was not safely quarantined\n");
        return 5;
    }
    if (!run_repeated_cancellation(module)) {
        fprintf(stderr, "capture cancellation was not retried safely\n");
        return 6;
    }
    if (!run_quarantine_cap(module)) {
        fprintf(stderr, "capture quarantine cap did not fail closed\n");
        return 7;
    }
    return 0;
}
