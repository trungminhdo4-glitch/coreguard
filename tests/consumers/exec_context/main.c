/* Public-consumer probe for cg_run_ex: execution context plumbing and the
   fail-closed context validation contract. Own work only; synthetic children. */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "coreguard.h"

#define CG_CONSUMER_TIMEOUT_MS UINT32_C(10000)

static int parse_positive_u64(const wchar_t *text, uint64_t *value_out)
{
    uint64_t value = 0U;
    size_t index;

    if (text == NULL || text[0] == L'\0') {
        return 0;
    }
    for (index = 0U; text[index] != L'\0'; index++) {
        unsigned int digit;

        if (text[index] < L'0' || text[index] > L'9') {
            return 0;
        }
        digit = (unsigned int)(text[index] - L'0');
        if (value > (UINT64_MAX - (uint64_t)digit) / UINT64_C(10)) {
            return 0;
        }
        value = value * UINT64_C(10) + (uint64_t)digit;
    }
    if (value == 0U) {
        return 0;
    }
    *value_out = value;
    return 1;
}

static void print_result(const cg_run_result *result)
{
    printf("api=0\n");
    printf("status=%s\n", cg_status_name(result->status));
    printf("has_exit_code=%d\n", result->has_exit_code ? 1 : 0);
    printf("exit_code=%lu\n", (unsigned long)result->exit_code);
    printf("output_truncated=%d\n", result->output_truncated ? 1 : 0);
    printf("cleanup_ok=%d\n", result->cleanup_ok ? 1 : 0);
    printf("win32_error=%lu\n", (unsigned long)result->win32_error);
    printf("stdout_size=%zu\n", result->stdout_size);
    printf("stderr_size=%zu\n", result->stderr_size);
    printf("stdout_begin\n");
    if (result->stdout_utf8 != NULL) {
        fwrite(result->stdout_utf8, 1U, result->stdout_size, stdout);
    }
    printf("stdout_end\n");
    fflush(stdout);
}

int wmain(int argc, wchar_t **argv)
{
    static const wchar_t empty_environment[] = L"\0\0";
    static const wchar_t unterminated_environment[] = L"A=1";
    static const wchar_t interior_environment[] = L"A=1\0\0B=2\0\0";
    cg_run_options options = {0};
    cg_exec_context context = {0};
    const cg_exec_context *context_ptr = NULL;
    cg_run_result result = {0};
    const wchar_t *mode;
    int rc;

    if (argc < 3) {
        fprintf(stderr, "usage: exec_context <mode> <child> [args...]\n");
        return 2;
    }
    mode = argv[1];
    options.argv = (const wchar_t *const *)&argv[2];
    options.argc = (size_t)(argc - 2);
    options.timeout_ms = CG_CONSUMER_TIMEOUT_MS;
    options.capture_output = 1;

    if (wcscmp(mode, L"null") == 0) {
        context_ptr = NULL;
    } else if (wcsncmp(mode, L"cwd:", 4) == 0) {
        context.working_directory = mode + 4;
        context_ptr = &context;
    } else if (wcscmp(mode, L"cwd-empty") == 0) {
        context.working_directory = L"";
        context_ptr = &context;
    } else if (wcscmp(mode, L"env-empty") == 0) {
        context.environment_block = empty_environment;
        context.environment_block_chars = 2U;
        context_ptr = &context;
    } else if (wcscmp(mode, L"env-bad-termination") == 0) {
        context.environment_block = unterminated_environment;
        context.environment_block_chars = 4U;
        context_ptr = &context;
    } else if (wcscmp(mode, L"env-bad-interior") == 0) {
        context.environment_block = interior_environment;
        context.environment_block_chars = 10U;
        context_ptr = &context;
    } else if (wcscmp(mode, L"chars-without-block") == 0) {
        context.environment_block = NULL;
        context.environment_block_chars = 5U;
        context_ptr = &context;
    } else if (wcsncmp(mode, L"limit:", 6) == 0) {
        uint64_t limit;

        if (!parse_positive_u64(mode + 6, &limit) ||
            limit > (uint64_t)CG_CAPTURE_PREFIX_MAX_BYTES) {
            fprintf(stderr, "invalid limit\n");
            return 2;
        }
        context.capture_prefix_bytes = (size_t)limit;
        context_ptr = &context;
    } else if (wcscmp(mode, L"limit-over-max") == 0) {
        context.capture_prefix_bytes = (size_t)CG_CAPTURE_PREFIX_MAX_BYTES + 1U;
        context_ptr = &context;
    } else {
        fprintf(stderr, "unknown mode\n");
        return 2;
    }

    rc = cg_run_ex(&options, context_ptr, &result);
    if (rc != 0) {
        fprintf(stderr, "cg_run_ex returned %d\n", rc);
        return 3;
    }
    print_result(&result);
    cg_run_result_free(&result);
    return 0;
}
