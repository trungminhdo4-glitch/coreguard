#include "coreguard.h"

#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CG_DEFAULT_TIMEOUT_MS 120000ULL
#define CG_TIMEOUT_EXIT_CODE 124
#define CG_RESOURCE_EXIT_CODE 123
#define CG_START_EXIT_CODE 125
#define CG_INTERNAL_EXIT_CODE 126

static void print_usage(FILE *stream)
{
    fprintf(stream,
            "Usage: coreguard run [--json] [--timeout-ms N] "
            "[--memory-limit-mb N] [--cpu-time-limit-ms N] "
            "[--max-processes N] [--cwd DIR] [--env-clear] "
            "[--env NAME=VALUE] [--capture-limit-bytes N] "
            "[--stdin-file PATH] -- command args...\n"
            "       coreguard --help\n\n"
            "Runs one executable directly and contains it in a Windows Job Object.\n"
            "--memory-limit-mb applies to the complete controlled process tree.\n"
            "--cpu-time-limit-ms applies job-wide user-mode CPU time.\n"
            "--max-processes applies to simultaneously active processes; the root counts.\n"
            "--cwd sets the child working directory; the default inherits the caller's.\n"
            "--env-clear starts an empty child environment; --env adds or overrides\n"
            "  one NAME=VALUE entry (repeatable; names match case-insensitively).\n"
            "--capture-limit-bytes bounds the retained capture prefix per stream\n"
            "  and requires --json.\n"
            "--stdin-file feeds a bounded payload (at most 64 KiB) to the child\n"
            "  stdin and then closes it; the default inherits the caller's stdin.\n");
}

typedef struct cg_env_pool {
    const wchar_t **items;
    size_t count;
    size_t capacity;
} cg_env_pool;

typedef struct cg_env_block {
    wchar_t *block;
    size_t chars;
    wchar_t *parent;
} cg_env_block;

static void cg_env_pool_release(cg_env_pool *pool)
{
    free((void *)pool->items);
    pool->items = NULL;
    pool->count = 0U;
    pool->capacity = 0U;
}

static int cg_env_pool_append(cg_env_pool *pool, const wchar_t *entry)
{
    if (pool->count == pool->capacity) {
        size_t capacity = pool->capacity == 0U ? 32U : pool->capacity * 2U;
        const wchar_t **items;

        if (capacity < pool->capacity) {
            return 0;
        }
        items = (const wchar_t **)realloc((void *)pool->items,
                                          capacity * sizeof(items[0]));
        if (items == NULL) {
            return 0;
        }
        pool->items = items;
        pool->capacity = capacity;
    }
    pool->items[pool->count] = entry;
    pool->count++;
    return 1;
}

static size_t cg_env_key_length(const wchar_t *assignment)
{
    size_t length = 0U;

    while (assignment[length] != L'\0' && assignment[length] != L'=') {
        length++;
    }
    return length;
}

static int cg_env_key_matches(const wchar_t *entry, const wchar_t *assignment,
                              size_t key_length)
{
    size_t index;

    if (key_length == 0U || key_length > (size_t)INT_MAX) {
        return 0;
    }
    for (index = 0U; index < key_length; index++) {
        if (entry[index] == L'\0') {
            return 0;
        }
    }
    if (CompareStringOrdinal(entry, (int)key_length, assignment,
                             (int)key_length, TRUE) != CSTR_EQUAL) {
        return 0;
    }
    return entry[key_length] == L'=';
}

static int cg_env_override_valid(const wchar_t *assignment)
{
    size_t key_length = cg_env_key_length(assignment);

    return key_length > 0U && key_length <= (size_t)INT_MAX &&
           assignment[key_length] == L'=';
}

static int cg_env_override_duplicate(const cg_env_pool *overrides,
                                     const wchar_t *assignment)
{
    size_t key_length = cg_env_key_length(assignment);
    size_t index;

    for (index = 0U; index < overrides->count; index++) {
        if (cg_env_key_matches(overrides->items[index], assignment,
                               key_length)) {
            return 1;
        }
    }
    return 0;
}

static int __cdecl cg_env_entry_compare(const void *left, const void *right)
{
    const wchar_t *left_entry = *(const wchar_t *const *)left;
    const wchar_t *right_entry = *(const wchar_t *const *)right;
    int result = CompareStringOrdinal(left_entry, -1, right_entry, -1, TRUE);

    if (result == CSTR_LESS_THAN) {
        return -1;
    }
    if (result == CSTR_GREATER_THAN) {
        return 1;
    }
    return 0;
}

static int cg_env_build_block(const cg_env_pool *pool, cg_env_block *out)
{
    size_t total;
    size_t index;
    size_t position = 0U;
    wchar_t *block;

    total = pool->count == 0U ? 2U : 1U;
    for (index = 0U; index < pool->count; index++) {
        size_t length = wcslen(pool->items[index]);

        if (total > (size_t)-1 - (length + 1U)) {
            return 0;
        }
        total += length + 1U;
    }
    block = (wchar_t *)malloc(total * sizeof(block[0]));
    if (block == NULL) {
        return 0;
    }
    for (index = 0U; index < pool->count; index++) {
        size_t length = wcslen(pool->items[index]);

        memcpy(block + position, pool->items[index],
               (length + 1U) * sizeof(block[0]));
        position += length + 1U;
    }
    block[position] = L'\0';
    position++;
    if (pool->count == 0U) {
        block[position] = L'\0';
        position++;
    }
    out->block = block;
    out->chars = position;
    return 1;
}

static int cg_env_prepare(const cg_env_pool *overrides, int clear,
                          cg_env_block *out)
{
    cg_env_pool merged = {0};
    const wchar_t *cursor;
    size_t index;
    int ok = 0;

    out->block = NULL;
    out->chars = 0U;
    out->parent = NULL;
    if (!clear) {
        out->parent = GetEnvironmentStringsW();
        if (out->parent == NULL) {
            return 0;
        }
        cursor = out->parent;
        while (*cursor != L'\0') {
            size_t length = wcslen(cursor);

            if (!cg_env_override_duplicate(overrides, cursor) &&
                !cg_env_pool_append(&merged, cursor)) {
                goto done;
            }
            cursor += length + 1U;
        }
    }
    for (index = 0U; index < overrides->count; index++) {
        if (!cg_env_pool_append(&merged, overrides->items[index])) {
            goto done;
        }
    }
    if (merged.count > 1U) {
        qsort(merged.items, merged.count, sizeof(merged.items[0]),
              cg_env_entry_compare);
    }
    ok = cg_env_build_block(&merged, out);
done:
    cg_env_pool_release(&merged);
    if (!ok && out->parent != NULL) {
        LocalFree(out->parent);
        out->parent = NULL;
    }
    return ok;
}

static int cg_stdin_file_read(const wchar_t *path, void **data_out,
                              size_t *size_out)
{
    HANDLE file;
    LARGE_INTEGER size;
    void *data;
    DWORD read = 0U;

    *data_out = NULL;
    *size_out = 0U;
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        return 0;
    }
    if (!GetFileSizeEx(file, &size) || size.QuadPart <= 0 ||
        size.QuadPart > (LONGLONG)CG_STDIN_MAX_BYTES) {
        CloseHandle(file);
        return 0;
    }
    data = malloc((size_t)size.QuadPart);
    if (data == NULL) {
        CloseHandle(file);
        return 0;
    }
    if (!ReadFile(file, data, (DWORD)size.QuadPart, &read, NULL) ||
        (size_t)read != (size_t)size.QuadPart) {
        free(data);
        CloseHandle(file);
        return 0;
    }
    CloseHandle(file);
    *data_out = data;
    *size_out = (size_t)read;
    return 1;
}

static void cg_env_block_release(cg_env_block *env)
{
    free(env->block);
    env->block = NULL;
    env->chars = 0U;
    if (env->parent != NULL) {
        LocalFree(env->parent);
        env->parent = NULL;
    }
}

static int parse_positive_uint64(const wchar_t *text, uint64_t maximum,
                                 uint64_t *value_out)
{
    uint64_t value = 0;
    size_t index;
    unsigned int digit;

    if (text == NULL || text[0] == L'\0') {
        return 0;
    }
    for (index = 0; text[index] != L'\0'; index++) {
        if (text[index] < L'0' || text[index] > L'9') {
            return 0;
        }
        digit = (unsigned int)(text[index] - L'0');
        if (value > (UINT64_MAX - (uint64_t)digit) / UINT64_C(10)) {
            return 0;
        }
        value = value * UINT64_C(10) + (uint64_t)digit;
    }
    if (value == 0U || value > maximum) {
        return 0;
    }
    *value_out = value;
    return 1;
}

static int parse_timeout(const wchar_t *text, uint64_t *value_out)
{
    return parse_positive_uint64(text, UINT32_MAX - UINT32_C(1), value_out);
}

static int parse_memory_limit_mb(const wchar_t *text,
                                  cg_resource_limits *limits)
{
    uint64_t value;
    const uint64_t bytes_per_mb = UINT64_C(1024) * UINT64_C(1024);

    if (!parse_positive_uint64(text, UINT64_MAX / bytes_per_mb, &value)) {
        return 0;
    }
    limits->memory_limit_bytes = value * bytes_per_mb;
    limits->valid_limits |= CG_RESOURCE_LIMIT_MEMORY;
    return 1;
}

static int parse_cpu_time_limit_ms(const wchar_t *text,
                                   cg_resource_limits *limits)
{
    uint64_t value;

    if (!parse_positive_uint64(text, CG_RESOURCE_CPU_TIME_MAX_MS, &value)) {
        return 0;
    }
    limits->cpu_time_limit_ms = value;
    limits->valid_limits |= CG_RESOURCE_LIMIT_CPU_TIME;
    return 1;
}

static int parse_active_process_limit(const wchar_t *text,
                                      cg_resource_limits *limits)
{
    uint64_t value;

    if (!parse_positive_uint64(text, UINT32_MAX, &value)) {
        return 0;
    }
    limits->active_process_limit = (uint32_t)value;
    limits->valid_limits |= CG_RESOURCE_LIMIT_ACTIVE_PROCESSES;
    return 1;
}

static size_t valid_utf8_width(const unsigned char *data, size_t remaining)
{
    unsigned char first = data[0];
    unsigned char second;

    if (first >= 0xc2U && first <= 0xdfU) {
        return remaining >= 2U && (data[1] & 0xc0U) == 0x80U ? 2U : 0U;
    }
    if (first >= 0xe0U && first <= 0xefU && remaining >= 3U) {
        second = data[1];
        if ((second & 0xc0U) != 0x80U ||
            (data[2] & 0xc0U) != 0x80U ||
            (first == 0xe0U && second < 0xa0U) ||
            (first == 0xedU && second >= 0xa0U)) {
            return 0U;
        }
        return 3U;
    }
    if (first >= 0xf0U && first <= 0xf4U && remaining >= 4U) {
        second = data[1];
        if ((second & 0xc0U) != 0x80U ||
            (data[2] & 0xc0U) != 0x80U ||
            (data[3] & 0xc0U) != 0x80U ||
            (first == 0xf0U && second < 0x90U) ||
            (first == 0xf4U && second >= 0x90U)) {
            return 0U;
        }
        return 4U;
    }
    return 0U;
}

static void print_json_string(FILE *stream, const char *data, size_t size)
{
    size_t i = 0;
    fputc('"', stream);
    while (i < size) {
        unsigned char c = (unsigned char)data[i];
        if (c == '"') {
            fputs("\\\"", stream);
            i++;
        } else if (c == '\\') {
            fputs("\\\\", stream);
            i++;
        } else if (c == '\n') {
            fputs("\\n", stream);
            i++;
        } else if (c == '\r') {
            fputs("\\r", stream);
            i++;
        } else if (c == '\t') {
            fputs("\\t", stream);
            i++;
        } else if (c < 0x20U) {
            fprintf(stream, "\\u%04x", (unsigned)c);
            i++;
        } else if (c < 0x80U) {
            fputc((int)c, stream);
            i++;
        } else {
            /* Preserve valid UTF-8 as-is; escape malformed bytes visibly. */
            size_t width = valid_utf8_width((const unsigned char *)data + i,
                                            size - i);
            if (width != 0) {
                fwrite(data + i, 1U, width, stream);
                i += width;
            } else {
                fprintf(stream, "\\u00%02x", (unsigned)c);
                i++;
            }
        }
    }
    fputc('"', stream);
}

static void print_json_metric(uint32_t valid_fields, uint32_t field,
                              uint64_t value)
{
    if ((valid_fields & field) != 0U) {
        printf("%llu", (unsigned long long)value);
    } else {
        printf("null");
    }
}

static const char *resource_limit_kind_name(uint32_t kind)
{
    switch (kind) {
    case CG_RESOURCE_LIMIT_KIND_MEMORY:
        return "memory";
    case CG_RESOURCE_LIMIT_KIND_CPU_TIME:
        return "cpu_time";
    case CG_RESOURCE_LIMIT_KIND_ACTIVE_PROCESSES:
        return "active_processes";
    case CG_RESOURCE_LIMIT_KIND_UNKNOWN:
        return "unknown";
    default:
        return NULL;
    }
}

static void print_json_job_metric(uint32_t valid_fields, uint32_t field,
                                  uint64_t value)
{
    print_json_metric(valid_fields, field, value);
}

static void print_json_job_metrics(const cg_job_metrics *metrics)
{
    printf("  \"job_metrics_scope\": \"job\",\n");
    printf("  \"job_metrics\": {\n");
    printf("    \"snapshot_available\": %s,\n",
           metrics->snapshot_available ? "true" : "false");
    printf("    \"valid_fields\": %lu,\n",
           (unsigned long)metrics->valid_fields);
    printf("    \"total_user_cpu_ms\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_TOTAL_USER_CPU_TIME,
                          metrics->total_user_cpu_ms);
    printf(",\n    \"total_kernel_cpu_ms\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_TOTAL_KERNEL_CPU_TIME,
                          metrics->total_kernel_cpu_ms);
    printf(",\n    \"total_page_faults\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_TOTAL_PAGE_FAULTS,
                          metrics->total_page_faults);
    printf(",\n    \"total_processes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_TOTAL_PROCESSES,
                          metrics->total_processes);
    printf(",\n    \"active_processes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_ACTIVE_PROCESSES,
                          metrics->active_processes);
    printf(",\n    \"total_terminated_processes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_TOTAL_TERMINATED_PROCESSES,
                          metrics->total_terminated_processes);
    printf(",\n    \"read_operations\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_READ_OPERATIONS,
                          metrics->read_operations);
    printf(",\n    \"write_operations\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_WRITE_OPERATIONS,
                          metrics->write_operations);
    printf(",\n    \"other_operations\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_OTHER_OPERATIONS,
                          metrics->other_operations);
    printf(",\n    \"read_bytes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_READ_BYTES,
                          metrics->read_bytes);
    printf(",\n    \"write_bytes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_WRITE_BYTES,
                          metrics->write_bytes);
    printf(",\n    \"other_bytes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_OTHER_BYTES,
                          metrics->other_bytes);
    printf(",\n    \"peak_job_memory_used_bytes\": ");
    print_json_job_metric(metrics->valid_fields,
                          CG_JOB_METRIC_PEAK_JOB_MEMORY_USED,
                          metrics->peak_job_memory_used_bytes);
    printf(",\n    \"query_error\": ");
    if (metrics->query_error == 0U) {
        printf("null\n");
    } else {
        printf("%lu\n", (unsigned long)metrics->query_error);
    }
    printf("  },\n");
}

static void print_json_result(const cg_run_result *result,
                              const cg_job_metrics *job_metrics)
{
    printf("{\n");
    printf("  \"status\": \"%s\",\n", cg_status_name(result->status));
    if (result->has_exit_code) {
        printf("  \"exit_code\": %lu,\n", (unsigned long)result->exit_code);
    } else {
        printf("  \"exit_code\": null,\n");
    }
    printf("  \"timed_out\": %s,\n", result->timed_out ? "true" : "false");
    printf("  \"resource_limit_hit\": %s,\n",
           result->resource_limit_hit ? "true" : "false");
    printf("  \"resource_limit_kind\": ");
    if (resource_limit_kind_name(result->resource_limit_kind) == NULL) {
        printf("null,\n");
    } else {
        printf("\"%s\",\n",
               resource_limit_kind_name(result->resource_limit_kind));
    }
    printf("  \"duration_ms\": %llu,\n",
           (unsigned long long)result->duration_ms);
    printf("  \"process_id\": %lu,\n", (unsigned long)result->process_id);
    printf("  \"cleanup_ok\": %s,\n", result->cleanup_ok ? "true" : "false");
    printf("  \"metrics_scope\": \"process\",\n");
    printf("  \"metrics\": {\n");
    printf("    \"creation_time_unix_100ns\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_CREATION_TIME,
                      result->metrics.creation_time_unix_100ns);
    printf(",\n    \"user_cpu_ms\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_USER_CPU_TIME,
                      result->metrics.user_cpu_ms);
    printf(",\n    \"kernel_cpu_ms\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_KERNEL_CPU_TIME,
                      result->metrics.kernel_cpu_ms);
    printf(",\n    \"total_cpu_ms\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_TOTAL_CPU_TIME,
                      result->metrics.total_cpu_ms);
    printf(",\n    \"peak_working_set_bytes\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_PEAK_WORKING_SET,
                      result->metrics.peak_working_set_bytes);
    printf(",\n    \"read_operations\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_READ_OPERATIONS,
                      result->metrics.read_operations);
    printf(",\n    \"write_operations\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_WRITE_OPERATIONS,
                      result->metrics.write_operations);
    printf(",\n    \"read_bytes\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_READ_BYTES,
                      result->metrics.read_bytes);
    printf(",\n    \"write_bytes\": ");
    print_json_metric(result->metrics.valid_fields,
                      CG_PROCESS_METRIC_WRITE_BYTES,
                      result->metrics.write_bytes);
    printf("\n  },\n");
    print_json_job_metrics(job_metrics);
    printf("  \"output_truncated\": %s,\n",
           result->output_truncated ? "true" : "false");
    printf("  \"stdout\": ");
    print_json_string(stdout, result->stdout_utf8 ? result->stdout_utf8 : "",
                      result->stdout_size);
    printf(",\n  \"stderr\": ");
    print_json_string(stdout, result->stderr_utf8 ? result->stderr_utf8 : "",
                      result->stderr_size);
    if (result->win32_error != 0) {
        printf(",\n  \"win32_error\": %lu\n",
               (unsigned long)result->win32_error);
    } else {
        printf("\n");
    }
    printf("}\n");
}

static int print_human_result(const cg_run_result *result)
{
    if (result->has_exit_code) {
        fprintf(stderr,
                "coreguard: status=%s exit_code=%lu duration_ms=%llu "
                "process_id=%lu cleanup_ok=%s\n",
                cg_status_name(result->status),
                (unsigned long)result->exit_code,
                (unsigned long long)result->duration_ms,
                (unsigned long)result->process_id,
                result->cleanup_ok ? "true" : "false");
    } else {
        fprintf(stderr,
                "coreguard: status=%s exit_code=null duration_ms=%llu "
                "process_id=%lu cleanup_ok=%s\n",
                cg_status_name(result->status),
                (unsigned long long)result->duration_ms,
                (unsigned long)result->process_id,
                result->cleanup_ok ? "true" : "false");
    }
    return 0;
}

int wmain(int argc, wchar_t **argv)
{
    int json = 0;
    int separator = -1;
    uint64_t timeout_ms = CG_DEFAULT_TIMEOUT_MS;
    int i;
    int memory_limit_set = 0;
    int cpu_time_limit_set = 0;
    int active_process_limit_set = 0;
    int env_clear = 0;
    int capture_limit_set = 0;
    const wchar_t *cwd = NULL;
    const wchar_t *stdin_file = NULL;
    void *stdin_data = NULL;
    size_t stdin_size = 0U;
    uint64_t capture_limit = 0;
    cg_resource_limits resource_limits = {0};
    cg_env_pool env_overrides = {0};
    cg_env_block env_block = {0};
    cg_exec_context context = {0};
    const cg_exec_context *context_ptr = NULL;
    cg_run_options options;
    cg_run_result result;
    cg_job_metrics job_metrics = {0};
    int rc;

    if (argc < 2 || wcscmp(argv[1], L"--help") == 0 ||
        wcscmp(argv[1], L"-h") == 0) {
        print_usage(stdout);
        return argc < 2 ? 2 : 0;
    }
    if (wcscmp(argv[1], L"run") != 0) {
        print_usage(stderr);
        return 2;
    }
    for (i = 2; i < argc; i++) {
        if (wcscmp(argv[i], L"--") == 0) {
            separator = i;
            break;
        }
        if (wcscmp(argv[i], L"--json") == 0) {
            json = 1;
        } else if (wcscmp(argv[i], L"--timeout-ms") == 0 && i + 1 < argc) {
            if (!parse_timeout(argv[++i], &timeout_ms)) {
                fprintf(stderr, "coreguard: invalid --timeout-ms\n");
                goto usage_error;
            }
        } else if (wcscmp(argv[i], L"--memory-limit-mb") == 0 &&
                   i + 1 < argc) {
            if (memory_limit_set ||
                !parse_memory_limit_mb(argv[++i], &resource_limits)) {
                fprintf(stderr, "coreguard: invalid --memory-limit-mb\n");
                goto usage_error;
            }
            memory_limit_set = 1;
        } else if (wcscmp(argv[i], L"--cpu-time-limit-ms") == 0 &&
                   i + 1 < argc) {
            if (cpu_time_limit_set ||
                !parse_cpu_time_limit_ms(argv[++i], &resource_limits)) {
                fprintf(stderr, "coreguard: invalid --cpu-time-limit-ms\n");
                goto usage_error;
            }
            cpu_time_limit_set = 1;
        } else if (wcscmp(argv[i], L"--max-processes") == 0 &&
                   i + 1 < argc) {
            if (active_process_limit_set ||
                !parse_active_process_limit(argv[++i], &resource_limits)) {
                fprintf(stderr, "coreguard: invalid --max-processes\n");
                goto usage_error;
            }
            active_process_limit_set = 1;
        } else if (wcscmp(argv[i], L"--cwd") == 0 && i + 1 < argc) {
            if (cwd != NULL || argv[i + 1][0] == L'\0') {
                fprintf(stderr, "coreguard: invalid --cwd\n");
                goto usage_error;
            }
            cwd = argv[++i];
        } else if (wcscmp(argv[i], L"--env-clear") == 0) {
            env_clear = 1;
        } else if (wcscmp(argv[i], L"--env") == 0 && i + 1 < argc) {
            const wchar_t *assignment = argv[++i];

            if (!cg_env_override_valid(assignment) ||
                cg_env_override_duplicate(&env_overrides, assignment) ||
                !cg_env_pool_append(&env_overrides, assignment)) {
                fprintf(stderr, "coreguard: invalid or duplicate --env\n");
                goto usage_error;
            }
        } else if (wcscmp(argv[i], L"--capture-limit-bytes") == 0 &&
                   i + 1 < argc) {
            if (capture_limit_set ||
                !parse_positive_uint64(argv[++i], CG_CAPTURE_PREFIX_MAX_BYTES,
                                       &capture_limit)) {
                fprintf(stderr,
                        "coreguard: invalid --capture-limit-bytes\n");
                goto usage_error;
            }
            capture_limit_set = 1;
        } else if (wcscmp(argv[i], L"--stdin-file") == 0 && i + 1 < argc) {
            if (stdin_file != NULL || argv[i + 1][0] == L'\0') {
                fprintf(stderr, "coreguard: invalid --stdin-file\n");
                goto usage_error;
            }
            stdin_file = argv[++i];
        } else {
            fprintf(stderr, "coreguard: unknown or incomplete option\n");
            goto usage_error;
        }
    }
    if (separator < 0 || separator + 1 >= argc) {
        fprintf(stderr, "coreguard: expected '-- command args...'\n");
        goto usage_error;
    }
    if (capture_limit_set && !json) {
        fprintf(stderr, "coreguard: --capture-limit-bytes requires --json\n");
        goto usage_error;
    }
    if (env_overrides.count > 0U || env_clear) {
        if (!cg_env_prepare(&env_overrides, env_clear, &env_block)) {
            cg_env_pool_release(&env_overrides);
            fprintf(stderr, "coreguard: could not build the environment\n");
            return CG_INTERNAL_EXIT_CODE;
        }
    }
    cg_env_pool_release(&env_overrides);
    if (stdin_file != NULL &&
        !cg_stdin_file_read(stdin_file, &stdin_data, &stdin_size)) {
        fprintf(stderr, "coreguard: invalid --stdin-file\n");
        goto usage_error;
    }

    options.argv = (const wchar_t *const *)&argv[separator + 1];
    options.argc = (size_t)(argc - separator - 1);
    options.timeout_ms = timeout_ms;
    options.capture_output = json;
    options.resource_limits = (memory_limit_set || cpu_time_limit_set ||
                               active_process_limit_set)
                                  ? &resource_limits
                                  : NULL;
    if (cwd != NULL || env_block.block != NULL || capture_limit_set ||
        stdin_data != NULL) {
        context.working_directory = cwd;
        context.environment_block = env_block.block;
        context.environment_block_chars = env_block.chars;
        context.capture_prefix_bytes =
            capture_limit_set ? (size_t)capture_limit : 0U;
        context.stdin_data = stdin_data;
        context.stdin_size = stdin_size;
        context_ptr = &context;
    }
    if (json) {
        rc = context_ptr != NULL
                 ? cg_run_ex_with_job_metrics(&options, context_ptr, &result,
                                              &job_metrics)
                 : cg_run_with_job_metrics(&options, &result, &job_metrics);
    } else {
        rc = context_ptr != NULL ? cg_run_ex(&options, context_ptr, &result)
                                 : cg_run(&options, &result);
    }
    if (rc != 0) {
        fprintf(stderr, "coreguard: internal API failure\n");
        cg_env_block_release(&env_block);
        free(stdin_data);
        return CG_INTERNAL_EXIT_CODE;
    }
    if (json) {
        print_json_result(&result, &job_metrics);
    } else {
        print_human_result(&result);
    }

    rc = 0;
    if (result.status == CG_STATUS_TIMEOUT) {
        rc = CG_TIMEOUT_EXIT_CODE;
    } else if (result.status == CG_STATUS_RESOURCE_LIMIT) {
        rc = CG_RESOURCE_EXIT_CODE;
    } else if (result.status == CG_STATUS_START_FAILED ||
               result.status == CG_STATUS_CONTAINMENT_FAILED) {
        rc = CG_START_EXIT_CODE;
    } else if (result.status != CG_STATUS_EXITED) {
        rc = CG_INTERNAL_EXIT_CODE;
    } else if (result.has_exit_code) {
        rc = (int)(result.exit_code & 0xffU);
    }
    cg_run_result_free(&result);
    cg_env_block_release(&env_block);
    free(stdin_data);
    return rc;

usage_error:
    cg_env_pool_release(&env_overrides);
    cg_env_block_release(&env_block);
    free(stdin_data);
    return 2;
}
