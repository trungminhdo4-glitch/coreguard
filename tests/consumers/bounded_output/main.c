#include "coreguard.h"

#include <windows.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define TEST_OUTPUT_LIMIT (1024U * 1024U)
#define TEST_NOISE_BYTES (TEST_OUTPUT_LIMIT + 65536U)
#define TEST_THREAD_WAIT_MS 10000U

typedef struct noise_writer {
    HANDLE destination;
    HANDLE start_event;
    DWORD total_bytes;
    DWORD seed;
} noise_writer;

static unsigned char noise_byte(DWORD offset, DWORD seed)
{
    return (unsigned char)('A' + ((offset + seed) % 23U));
}

static DWORD WINAPI write_noise(LPVOID parameter)
{
    noise_writer *writer;
    unsigned char buffer[4096];
    DWORD offset = 0U;

    if (parameter == NULL) {
        return ERROR_INVALID_PARAMETER;
    }
    writer = (noise_writer *)parameter;
    if (WaitForSingleObject(writer->start_event, TEST_THREAD_WAIT_MS) !=
        WAIT_OBJECT_0) {
        return ERROR_GEN_FAILURE;
    }
    while (offset < writer->total_bytes) {
        DWORD count = writer->total_bytes - offset;
        DWORD index;
        DWORD written = 0U;

        if (count > (DWORD)sizeof(buffer)) {
            count = (DWORD)sizeof(buffer);
        }
        for (index = 0U; index < count; index++) {
            buffer[index] = noise_byte(offset + index, writer->seed);
        }
        if (!WriteFile(writer->destination, buffer, count, &written, NULL) ||
            written == 0U) {
            DWORD error = GetLastError();
            return error == ERROR_SUCCESS ? ERROR_WRITE_FAULT : error;
        }
        offset += written;
    }
    return ERROR_SUCCESS;
}

static int signal_output_complete(const wchar_t *event_name)
{
    HANDLE event = OpenEventW(EVENT_MODIFY_STATE, FALSE, event_name);
    int succeeded;

    if (event == NULL) {
        return 0;
    }
    succeeded = SetEvent(event) != 0;
    CloseHandle(event);
    return succeeded;
}

static int wait_for_noise_threads(HANDLE *threads)
{
    DWORD wait_result;

    if (threads == NULL || threads[0] == NULL || threads[1] == NULL) {
        return 0;
    }
    wait_result = WaitForMultipleObjects(2, threads, TRUE,
                                         TEST_THREAD_WAIT_MS);

    if (wait_result == WAIT_OBJECT_0) {
        return 1;
    }
    (void)CancelSynchronousIo(threads[0]);
    (void)CancelSynchronousIo(threads[1]);
    return WaitForMultipleObjects(2, threads, TRUE, 2000U) == WAIT_OBJECT_0;
}

static int run_noise_payload(DWORD total_bytes, const wchar_t *event_name)
{
    HANDLE start_event;
    HANDLE threads[2];
    noise_writer writers[2];
    DWORD wait_result;
    DWORD exit_code;
    int succeeded = 1;

    start_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (start_event == NULL) {
        return 20;
    }
    writers[0].destination = GetStdHandle(STD_OUTPUT_HANDLE);
    writers[0].start_event = start_event;
    writers[0].total_bytes = total_bytes;
    writers[0].seed = 3U;
    writers[1].destination = GetStdHandle(STD_ERROR_HANDLE);
    writers[1].start_event = start_event;
    writers[1].total_bytes = total_bytes;
    writers[1].seed = 11U;
    threads[0] = CreateThread(NULL, 0, write_noise, &writers[0], 0, NULL);
    threads[1] = CreateThread(NULL, 0, write_noise, &writers[1], 0, NULL);
    if (threads[0] == NULL || threads[1] == NULL) {
        (void)SetEvent(start_event);
        if (threads[0] != NULL) {
            (void)CancelSynchronousIo(threads[0]);
            if (WaitForSingleObject(threads[0], 2000U) != WAIT_OBJECT_0) {
                ExitProcess(21U);
            }
            CloseHandle(threads[0]);
        }
        if (threads[1] != NULL) {
            (void)CancelSynchronousIo(threads[1]);
            if (WaitForSingleObject(threads[1], 2000U) != WAIT_OBJECT_0) {
                ExitProcess(21U);
            }
            CloseHandle(threads[1]);
        }
        CloseHandle(start_event);
        return 21;
    }
    if (!SetEvent(start_event)) {
        succeeded = 0;
    }
    wait_result = wait_for_noise_threads(threads) ? WAIT_OBJECT_0 : WAIT_TIMEOUT;
    if (wait_result != WAIT_OBJECT_0) {
        ExitProcess(22U);
    }
    if (!GetExitCodeThread(threads[0], &exit_code) ||
        exit_code != ERROR_SUCCESS) {
        succeeded = 0;
    }
    if (!GetExitCodeThread(threads[1], &exit_code) ||
        exit_code != ERROR_SUCCESS) {
        succeeded = 0;
    }
    CloseHandle(threads[0]);
    CloseHandle(threads[1]);
    CloseHandle(start_event);
    if (!signal_output_complete(event_name)) {
        succeeded = 0;
    }
    return succeeded ? 0 : 22;
}

static int duplicate_inheritable(HANDLE source, HANDLE *duplicate_out)
{
    *duplicate_out = NULL;
    if (source == NULL || source == INVALID_HANDLE_VALUE) {
        return 0;
    }
    return DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(),
                           duplicate_out, 0, TRUE, DUPLICATE_SAME_ACCESS) != 0;
}

static int retain_writers_in_process(DWORD process_id)
{
    HANDLE target_process;
    HANDLE stdout_handle = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE stderr_handle = GetStdHandle(STD_ERROR_HANDLE);
    HANDLE retained_stdout = NULL;
    HANDLE retained_stderr = NULL;
    int succeeded;

    if (stdout_handle == NULL || stdout_handle == INVALID_HANDLE_VALUE ||
        stderr_handle == NULL || stderr_handle == INVALID_HANDLE_VALUE) {
        return 0;
    }
    target_process = OpenProcess(PROCESS_DUP_HANDLE, FALSE, process_id);
    if (target_process == NULL) {
        return 0;
    }
    succeeded =
        DuplicateHandle(GetCurrentProcess(), stdout_handle,
                        target_process, &retained_stdout, 0, FALSE,
                        DUPLICATE_SAME_ACCESS) &&
        DuplicateHandle(GetCurrentProcess(), stderr_handle,
                        target_process, &retained_stderr, 0, FALSE,
                        DUPLICATE_SAME_ACCESS);
    succeeded = succeeded && retained_stdout != NULL &&
                retained_stderr != NULL;
    /* These numeric handle values belong to the target process. */
    CloseHandle(target_process);
    return succeeded;
}

static int run_timeout_root(const wchar_t *event_name)
{
    wchar_t module[MAX_PATH];
    wchar_t command[MAX_PATH + 160];
    DWORD module_length;
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    HANDLE child_stdin = NULL;
    HANDLE child_stdout = NULL;
    HANDLE child_stderr = NULL;
    int created;
    SECURITY_ATTRIBUTES attributes;

    module_length = GetModuleFileNameW(NULL, module, (DWORD)_countof(module));
    if (module_length == 0U || module_length >= (DWORD)_countof(module)) {
        return 30;
    }
    if (swprintf_s(command, _countof(command),
                   L"\"%ls\" --timeout-child %ls", module,
                   event_name) < 0) {
        return 31;
    }
    ZeroMemory(&attributes, sizeof(attributes));
    attributes.nLength = sizeof(attributes);
    attributes.bInheritHandle = TRUE;
    child_stdin = CreateFileW(L"NUL", GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, &attributes,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (child_stdin == INVALID_HANDLE_VALUE) {
        child_stdin = NULL;
    }
    if (child_stdin == NULL ||
        !duplicate_inheritable(GetStdHandle(STD_OUTPUT_HANDLE), &child_stdout) ||
        !duplicate_inheritable(GetStdHandle(STD_ERROR_HANDLE), &child_stderr)) {
        if (child_stdin != NULL) {
            CloseHandle(child_stdin);
        }
        if (child_stdout != NULL) {
            CloseHandle(child_stdout);
        }
        if (child_stderr != NULL) {
            CloseHandle(child_stderr);
        }
        return 32;
    }
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = child_stdin;
    startup.hStdOutput = child_stdout;
    startup.hStdError = child_stderr;
    created = CreateProcessW(NULL, command, NULL, NULL, TRUE, 0, NULL, NULL,
                             &startup, &process) != 0;
    CloseHandle(child_stdin);
    CloseHandle(child_stdout);
    CloseHandle(child_stderr);
    if (!created) {
        return 33;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    Sleep(INFINITE);
    return 34;
}

static int prefix_is_exact(const char *data, size_t size, DWORD seed)
{
    size_t index;

    if (data == NULL || size != (size_t)TEST_OUTPUT_LIMIT) {
        return 0;
    }
    for (index = 0U; index < size; index++) {
        if ((unsigned char)data[index] != noise_byte((DWORD)index, seed)) {
            return 0;
        }
    }
    return 1;
}

static int run_case(const wchar_t *module, const wchar_t *mode,
                    uint64_t timeout_ms, cg_status expected_status,
                    int expected_truncated, int retain_writers)
{
    const wchar_t *arguments[4];
    wchar_t event_name[96];
    wchar_t process_id[32];
    cg_run_options options;
    cg_run_result result;
    HANDLE output_complete;
    ULONGLONG started;
    ULONGLONG elapsed;
    size_t argument_count = 3U;
    int valid;

    if (swprintf_s(event_name, _countof(event_name),
                   L"Local\\CoreguardOutput-%lu-%llu",
                   GetCurrentProcessId(), GetTickCount64()) < 0) {
        return 0;
    }
    output_complete = CreateEventW(NULL, TRUE, FALSE, event_name);
    if (output_complete == NULL) {
        return 0;
    }
    arguments[0] = module;
    arguments[1] = mode;
    arguments[2] = event_name;
    if (retain_writers) {
        if (swprintf_s(process_id, _countof(process_id), L"%lu",
                       GetCurrentProcessId()) < 0) {
            CloseHandle(output_complete);
            return 0;
        }
        arguments[3] = process_id;
        argument_count = 4U;
    }
    ZeroMemory(&options, sizeof(options));
    ZeroMemory(&result, sizeof(result));
    options.argv = arguments;
    options.argc = argument_count;
    options.timeout_ms = timeout_ms;
    options.capture_output = 1;
    started = GetTickCount64();
    if (cg_run(&options, &result) != 0) {
        CloseHandle(output_complete);
        return 0;
    }
    elapsed = GetTickCount64() - started;
    valid = result.status == expected_status && result.cleanup_ok &&
            result.output_truncated == expected_truncated &&
            prefix_is_exact(result.stdout_utf8, result.stdout_size, 3U) &&
            prefix_is_exact(result.stderr_utf8, result.stderr_size, 11U) &&
            WaitForSingleObject(output_complete, 0) == WAIT_OBJECT_0;
    if (expected_status == CG_STATUS_EXITED) {
        valid = valid && result.has_exit_code && result.exit_code == 0U &&
                !result.timed_out;
    } else {
        valid = valid && result.timed_out && !result.has_exit_code &&
                elapsed < 7000U;
    }
    if (retain_writers) {
        valid = valid && elapsed < 5000U;
    }
    cg_run_result_free(&result);
    CloseHandle(output_complete);
    return valid;
}

int wmain(int argc, wchar_t **argv)
{
    wchar_t module[MAX_PATH];
    DWORD module_length;

    if (argc == 3 && wcscmp(argv[1], L"--noise") == 0) {
        return run_noise_payload(TEST_NOISE_BYTES, argv[2]);
    }
    if (argc == 3 && wcscmp(argv[1], L"--exact") == 0) {
        return run_noise_payload(TEST_OUTPUT_LIMIT, argv[2]);
    }
    if (argc == 4 && wcscmp(argv[1], L"--retained") == 0) {
        wchar_t *end = NULL;
        unsigned long process_id = wcstoul(argv[3], &end, 10);
        if (end == argv[3] || *end != L'\0' ||
            !retain_writers_in_process((DWORD)process_id)) {
            return 41;
        }
        return run_noise_payload(TEST_NOISE_BYTES, argv[2]);
    }
    if (argc == 3 && wcscmp(argv[1], L"--timeout-root") == 0) {
        return run_timeout_root(argv[2]);
    }
    if (argc == 3 && wcscmp(argv[1], L"--timeout-child") == 0) {
        int result = run_noise_payload(TEST_NOISE_BYTES, argv[2]);
        if (result != 0) {
            return result;
        }
        Sleep(INFINITE);
        return 40;
    }
    module_length = GetModuleFileNameW(NULL, module, (DWORD)_countof(module));
    if (module_length == 0U || module_length >= (DWORD)_countof(module)) {
        return 1;
    }
    if (!run_case(module, L"--noise", 5000U, CG_STATUS_EXITED, 1, 0)) {
        fprintf(stderr, "bounded concurrent output case failed\n");
        return 2;
    }
    if (!run_case(module, L"--exact", 5000U, CG_STATUS_EXITED, 0, 0)) {
        fprintf(stderr, "exact-limit output case failed\n");
        return 3;
    }
    if (!run_case(module, L"--retained", 5000U, CG_STATUS_EXITED, 1, 1)) {
        fprintf(stderr, "retained-writer cancellation case failed\n");
        return 4;
    }
    if (!run_case(module, L"--timeout-root", 2000U, CG_STATUS_TIMEOUT, 1, 0)) {
        fprintf(stderr, "descendant timeout output case failed\n");
        return 5;
    }
    return 0;
}
