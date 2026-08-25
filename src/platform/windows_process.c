#include "process.h"
#include "platform/windows_process_test.h"

#include <windows.h>
#include <psapi.h>

#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#pragma comment(lib, "psapi.lib")

#define CG_MAX_COMMAND_LINE 32767U
#define CG_OUTPUT_LIMIT (1024U * 1024U)
#define CG_TIMEOUT_GRACE_MS 5000U
#define CG_RESOURCE_POLL_MS 5U
#define CG_RESOURCE_TERMINATION_CODE 123U
#define CG_WAIT_RESOURCE 0x10000U
#define CG_RESOURCE_FLAG_MEMORY 0x1U
#define CG_RESOURCE_FLAG_CPU_TIME 0x2U
#define CG_RESOURCE_FLAG_ACTIVE_PROCESSES 0x4U
#define CG_FILETIME_UNIX_EPOCH_100NS UINT64_C(116444736000000000)
#define CG_100NS_PER_MS UINT64_C(10000)

typedef struct cg_wbuilder {
    wchar_t *data;
    size_t length;
    size_t capacity;
} cg_wbuilder;

typedef struct cg_capture_file {
    HANDLE handle;
    wchar_t path[MAX_PATH];
    int active;
} cg_capture_file;

_Static_assert(sizeof(SIZE_T) <= sizeof(uint64_t),
               "SIZE_T must fit in the public metric representation");
_Static_assert(sizeof(ULONGLONG) <= sizeof(uint64_t),
               "ULONGLONG must fit in the public metric representation");

static uint64_t cg_now_ms(void)
{
    static LARGE_INTEGER frequency;
    LARGE_INTEGER counter;
    uint64_t whole;
    uint64_t remainder;

    if (frequency.QuadPart == 0) {
        if (!QueryPerformanceFrequency(&frequency)) {
            return GetTickCount64();
        }
    }
    if (!QueryPerformanceCounter(&counter)) {
        return GetTickCount64();
    }
    whole = (uint64_t)(counter.QuadPart / frequency.QuadPart);
    remainder = (uint64_t)(counter.QuadPart % frequency.QuadPart);
    return whole * 1000U + (remainder * 1000U) / (uint64_t)frequency.QuadPart;
}

static void cg_set_error(cg_run_result *result, DWORD error)
{
    result->win32_error = (uint32_t)error;
}

static int cg_memory_limit_enabled(const cg_resource_limits *limits)
{
    return limits != NULL &&
           (limits->valid_limits & CG_RESOURCE_LIMIT_MEMORY) != 0U;
}

static int cg_cpu_time_limit_enabled(const cg_resource_limits *limits)
{
    return limits != NULL &&
           (limits->valid_limits & CG_RESOURCE_LIMIT_CPU_TIME) != 0U;
}

static int cg_active_process_limit_enabled(const cg_resource_limits *limits)
{
    return limits != NULL &&
           (limits->valid_limits & CG_RESOURCE_LIMIT_ACTIVE_PROCESSES) != 0U;
}

static int cg_cpu_time_limit_ticks(uint64_t milliseconds,
                                   uint64_t *ticks_out)
{
    uint64_t ticks;

    if (milliseconds == 0U ||
        milliseconds > CG_RESOURCE_CPU_TIME_MAX_MS) {
        return 0;
    }
    ticks = milliseconds * CG_100NS_PER_MS;
    if (ticks > UINT64_C(0x7fffffffffffffff)) {
        return 0;
    }
    *ticks_out = ticks;
    return 1;
}

static uint32_t cg_resource_limit_kind(uint32_t resource_flags)
{
    if (resource_flags == CG_RESOURCE_FLAG_MEMORY) {
        return CG_RESOURCE_LIMIT_KIND_MEMORY;
    }
    if (resource_flags == CG_RESOURCE_FLAG_CPU_TIME) {
        return CG_RESOURCE_LIMIT_KIND_CPU_TIME;
    }
    if (resource_flags == CG_RESOURCE_FLAG_ACTIVE_PROCESSES) {
        return CG_RESOURCE_LIMIT_KIND_ACTIVE_PROCESSES;
    }
    return resource_flags == 0U ? CG_RESOURCE_LIMIT_KIND_NONE
                                : CG_RESOURCE_LIMIT_KIND_UNKNOWN;
}

static int cg_create_resource_monitor(HANDLE job, int memory_limit_enabled,
                                      uint64_t memory_limit_bytes,
                                      HANDLE *completion_port_out,
                                      DWORD *error_out)
{
    HANDLE completion_port;
    JOBOBJECT_ASSOCIATE_COMPLETION_PORT association;
    JOBOBJECT_NOTIFICATION_LIMIT_INFORMATION notification;

    *completion_port_out = NULL;
    *error_out = ERROR_SUCCESS;
    completion_port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, NULL, 0, 1);
    if (completion_port == NULL) {
        *error_out = GetLastError();
        return 0;
    }

    ZeroMemory(&association, sizeof(association));
    association.CompletionPort = completion_port;
    if (!SetInformationJobObject(job, JobObjectAssociateCompletionPortInformation,
                                  &association, sizeof(association))) {
        *error_out = GetLastError();
        CloseHandle(completion_port);
        return 0;
    }

    if (memory_limit_enabled) {
        ZeroMemory(&notification, sizeof(notification));
        notification.JobMemoryLimit = (DWORD64)memory_limit_bytes;
        notification.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY;
        if (!SetInformationJobObject(job, JobObjectNotificationLimitInformation,
                                     &notification, sizeof(notification))) {
            *error_out = GetLastError();
            CloseHandle(completion_port);
            return 0;
        }
    }
    *completion_port_out = completion_port;
    return 1;
}

static int cg_drain_resource_notifications(HANDLE completion_port,
                                           uint32_t *resource_flags,
                                           DWORD *error_out)
{
    DWORD message;
    ULONG_PTR completion_key;
    LPOVERLAPPED overlapped;

    if (completion_port == NULL) {
        return 1;
    }
    for (;;) {
        completion_key = 0;
        overlapped = NULL;
        if (!GetQueuedCompletionStatus(completion_port, &message,
                                       &completion_key, &overlapped, 0)) {
            *error_out = GetLastError();
            if (*error_out == WAIT_TIMEOUT) {
                return 1;
            }
            return 0;
        }
        if (message == JOB_OBJECT_MSG_JOB_MEMORY_LIMIT ||
            message == JOB_OBJECT_MSG_NOTIFICATION_LIMIT) {
            *resource_flags |= CG_RESOURCE_FLAG_MEMORY;
        } else if (message == JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT) {
            *resource_flags |= CG_RESOURCE_FLAG_ACTIVE_PROCESSES;
        }
    }
}

static int cg_check_cpu_job_signal(HANDLE job, int enabled,
                                   uint32_t *resource_flags,
                                   DWORD *error_out)
{
    DWORD wait_result;

    if (!enabled) {
        return 1;
    }
    wait_result = WaitForSingleObject(job, 0);
    if (wait_result == WAIT_FAILED) {
        *error_out = GetLastError();
        return 0;
    }
    if (wait_result == WAIT_OBJECT_0) {
        *resource_flags |= CG_RESOURCE_FLAG_CPU_TIME;
    }
    return 1;
}

static int cg_check_cpu_accounting(HANDLE job, uint64_t limit_ticks,
                                   uint32_t *resource_flags,
                                   DWORD *error_out)
{
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting;
    DWORD returned;

    if (limit_ticks == 0U) {
        return 1;
    }
    ZeroMemory(&accounting, sizeof(accounting));
    if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                   &accounting, sizeof(accounting),
                                   &returned)) {
        *error_out = GetLastError();
        return 0;
    }
    if (accounting.TotalUserTime.QuadPart > 0 &&
        (uint64_t)accounting.TotalUserTime.QuadPart > limit_ticks) {
        *resource_flags |= CG_RESOURCE_FLAG_CPU_TIME;
    }
    return 1;
}

static DWORD cg_wait_for_process(HANDLE process, HANDLE completion_port,
                                 HANDLE job, uint64_t cpu_time_limit_ticks,
                                 int poll_completion_port, DWORD timeout_ms,
                                 uint32_t *resource_flags,
                                 DWORD *error_out)
{
    uint64_t deadline = cg_now_ms() + (uint64_t)timeout_ms;

    if (!poll_completion_port && cpu_time_limit_ticks == 0U) {
        return WaitForSingleObject(process, timeout_ms);
    }

    for (;;) {
        uint64_t now = cg_now_ms();
        uint64_t remaining;
        DWORD wait_ms;
        DWORD wait_result;
        if (!cg_check_cpu_job_signal(job, cpu_time_limit_ticks != 0U,
                                     resource_flags, error_out)) {
            return WAIT_FAILED;
        }
        if (!cg_check_cpu_accounting(job, cpu_time_limit_ticks,
                                     resource_flags, error_out)) {
            return WAIT_FAILED;
        }
        if (*resource_flags != 0U) {
            return CG_WAIT_RESOURCE;
        }

        if (now >= deadline) {
            wait_result = WaitForSingleObject(process, 0);
            if (wait_result == WAIT_FAILED) {
                *error_out = GetLastError();
                return WAIT_FAILED;
            }
            if (!cg_drain_resource_notifications(completion_port,
                                                 resource_flags, error_out)) {
                return WAIT_FAILED;
            }
            if (!cg_check_cpu_job_signal(job, cpu_time_limit_ticks != 0U,
                                         resource_flags, error_out)) {
                return WAIT_FAILED;
            }
            if (!cg_check_cpu_accounting(job, cpu_time_limit_ticks,
                                         resource_flags, error_out)) {
                return WAIT_FAILED;
            }
            if (*resource_flags != 0U) {
                return CG_WAIT_RESOURCE;
            }
            return wait_result == WAIT_OBJECT_0 ? WAIT_OBJECT_0 : WAIT_TIMEOUT;
        }

        remaining = deadline - now;
        wait_ms = remaining > CG_RESOURCE_POLL_MS
                      ? CG_RESOURCE_POLL_MS
                      : (DWORD)remaining;
        if (wait_ms == 0U) {
            wait_ms = 1U;
        }
        if (!poll_completion_port && cpu_time_limit_ticks != 0U) {
            HANDLE wait_handles[2];
            wait_handles[0] = process;
            wait_handles[1] = job;
            wait_result = WaitForMultipleObjects(2, wait_handles, FALSE,
                                                 wait_ms);
            if (wait_result == WAIT_OBJECT_0 + 1U) {
                *resource_flags |= CG_RESOURCE_FLAG_CPU_TIME;
                return CG_WAIT_RESOURCE;
            }
        } else {
            wait_result = WaitForSingleObject(process, wait_ms);
        }
        if (wait_result == WAIT_FAILED) {
            *error_out = GetLastError();
            return WAIT_FAILED;
        }
        if (poll_completion_port &&
            !cg_drain_resource_notifications(completion_port, resource_flags,
                                             error_out)) {
            return WAIT_FAILED;
        }
        if (!cg_check_cpu_job_signal(job, cpu_time_limit_ticks != 0U,
                                     resource_flags, error_out)) {
            return WAIT_FAILED;
        }
        if (!cg_check_cpu_accounting(job, cpu_time_limit_ticks,
                                     resource_flags, error_out)) {
            return WAIT_FAILED;
        }
        if (*resource_flags != 0U) {
            return CG_WAIT_RESOURCE;
        }
        if (wait_result == WAIT_OBJECT_0) {
            return WAIT_OBJECT_0;
        }
    }
}

static uint64_t cg_filetime_value(const FILETIME *value)
{
    return ((uint64_t)value->dwHighDateTime << 32) |
           (uint64_t)value->dwLowDateTime;
}

static int cg_u64_add(uint64_t left, uint64_t right, uint64_t *value_out)
{
    if (right > UINT64_MAX - left) {
        return 0;
    }
    *value_out = left + right;
    return 1;
}

static BOOL WINAPI cg_query_job_information(
    HANDLE job,
    JOBOBJECTINFOCLASS information_class,
    LPVOID information,
    DWORD information_length,
    LPDWORD returned_length)
{
    return QueryInformationJobObject(job, information_class, information,
                                     information_length, returned_length);
}

static void cg_record_job_metrics_query_error(cg_job_metrics *metrics)
{
    DWORD error = GetLastError();

    if (error == ERROR_SUCCESS) {
        error = ERROR_GEN_FAILURE;
    }
    if (metrics->query_error == 0U) {
        metrics->query_error = (uint32_t)error;
    }
}

static uint64_t cg_job_time_ms(const LARGE_INTEGER *value)
{
    if (value->QuadPart <= 0) {
        return 0U;
    }
    return (uint64_t)value->QuadPart / CG_100NS_PER_MS;
}

static void cg_collect_job_metrics(HANDLE job,
                                   cg_job_metrics *metrics,
                                   cg_query_job_information_fn query_job)
{
    JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION accounting;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    DWORD returned;
    int accounting_available = 0;
    int limits_available = 0;

    ZeroMemory(metrics, sizeof(*metrics));
    ZeroMemory(&accounting, sizeof(accounting));
    if (query_job(job, JobObjectBasicAndIoAccountingInformation,
                  &accounting, sizeof(accounting), &returned)) {
        metrics->total_user_cpu_ms =
            cg_job_time_ms(&accounting.BasicInfo.TotalUserTime);
        metrics->total_kernel_cpu_ms =
            cg_job_time_ms(&accounting.BasicInfo.TotalKernelTime);
        metrics->total_page_faults = accounting.BasicInfo.TotalPageFaultCount;
        metrics->total_processes = accounting.BasicInfo.TotalProcesses;
        metrics->active_processes = accounting.BasicInfo.ActiveProcesses;
        metrics->total_terminated_processes =
            accounting.BasicInfo.TotalTerminatedProcesses;
        metrics->read_operations = accounting.IoInfo.ReadOperationCount;
        metrics->write_operations = accounting.IoInfo.WriteOperationCount;
        metrics->other_operations = accounting.IoInfo.OtherOperationCount;
        metrics->read_bytes = accounting.IoInfo.ReadTransferCount;
        metrics->write_bytes = accounting.IoInfo.WriteTransferCount;
        metrics->other_bytes = accounting.IoInfo.OtherTransferCount;
        metrics->valid_fields = CG_JOB_METRIC_TOTAL_USER_CPU_TIME |
                                CG_JOB_METRIC_TOTAL_KERNEL_CPU_TIME |
                                CG_JOB_METRIC_TOTAL_PAGE_FAULTS |
                                CG_JOB_METRIC_TOTAL_PROCESSES |
                                CG_JOB_METRIC_ACTIVE_PROCESSES |
                                CG_JOB_METRIC_TOTAL_TERMINATED_PROCESSES |
                                CG_JOB_METRIC_READ_OPERATIONS |
                                CG_JOB_METRIC_WRITE_OPERATIONS |
                                CG_JOB_METRIC_OTHER_OPERATIONS |
                                CG_JOB_METRIC_READ_BYTES |
                                CG_JOB_METRIC_WRITE_BYTES |
                                CG_JOB_METRIC_OTHER_BYTES;
        accounting_available = 1;
    } else {
        cg_record_job_metrics_query_error(metrics);
    }

    ZeroMemory(&limits, sizeof(limits));
    if (query_job(job, JobObjectExtendedLimitInformation, &limits,
                  sizeof(limits), &returned)) {
        metrics->peak_job_memory_used_bytes =
            (uint64_t)limits.PeakJobMemoryUsed;
        metrics->valid_fields |= CG_JOB_METRIC_PEAK_JOB_MEMORY_USED;
        limits_available = 1;
    } else {
        cg_record_job_metrics_query_error(metrics);
    }
    metrics->snapshot_available =
        (uint32_t)(accounting_available || limits_available);
}

static void cg_collect_process_metrics(HANDLE process,
                                       cg_process_metrics *metrics)
{
    FILETIME creation_time;
    FILETIME exit_time;
    FILETIME kernel_time;
    FILETIME user_time;
    PROCESS_MEMORY_COUNTERS memory;
    IO_COUNTERS io;
    uint64_t creation_ticks;
    uint64_t user_ticks;
    uint64_t kernel_ticks;
    uint64_t user_ms;
    uint64_t kernel_ms;
    uint64_t total_ms;

    ZeroMemory(metrics, sizeof(*metrics));
    if (GetProcessTimes(process, &creation_time, &exit_time, &kernel_time,
                        &user_time)) {
        creation_ticks = cg_filetime_value(&creation_time);
        user_ticks = cg_filetime_value(&user_time);
        kernel_ticks = cg_filetime_value(&kernel_time);
        if (creation_ticks >= CG_FILETIME_UNIX_EPOCH_100NS) {
            metrics->creation_time_unix_100ns =
                creation_ticks - CG_FILETIME_UNIX_EPOCH_100NS;
            metrics->valid_fields |= CG_PROCESS_METRIC_CREATION_TIME;
        }
        user_ms = user_ticks / CG_100NS_PER_MS;
        kernel_ms = kernel_ticks / CG_100NS_PER_MS;
        if (cg_u64_add(user_ms, kernel_ms, &total_ms)) {
            metrics->user_cpu_ms = user_ms;
            metrics->kernel_cpu_ms = kernel_ms;
            metrics->total_cpu_ms = total_ms;
            metrics->valid_fields |= CG_PROCESS_METRIC_USER_CPU_TIME |
                                     CG_PROCESS_METRIC_KERNEL_CPU_TIME |
                                     CG_PROCESS_METRIC_TOTAL_CPU_TIME;
        }
    }

    ZeroMemory(&memory, sizeof(memory));
    memory.cb = sizeof(memory);
    if (GetProcessMemoryInfo(process, &memory, sizeof(memory))) {
        metrics->peak_working_set_bytes = (uint64_t)memory.PeakWorkingSetSize;
        metrics->valid_fields |= CG_PROCESS_METRIC_PEAK_WORKING_SET;
    }

    ZeroMemory(&io, sizeof(io));
    if (GetProcessIoCounters(process, &io)) {
        metrics->read_operations = (uint64_t)io.ReadOperationCount;
        metrics->write_operations = (uint64_t)io.WriteOperationCount;
        metrics->read_bytes = (uint64_t)io.ReadTransferCount;
        metrics->write_bytes = (uint64_t)io.WriteTransferCount;
        metrics->valid_fields |= CG_PROCESS_METRIC_READ_OPERATIONS |
                                 CG_PROCESS_METRIC_WRITE_OPERATIONS |
                                 CG_PROCESS_METRIC_READ_BYTES |
                                 CG_PROCESS_METRIC_WRITE_BYTES;
    }
}

static int cg_builder_init(cg_wbuilder *builder)
{
    builder->data = (wchar_t *)malloc(256U * sizeof(wchar_t));
    if (builder->data == NULL) {
        builder->length = 0;
        builder->capacity = 0;
        return 0;
    }
    builder->data[0] = L'\0';
    builder->length = 0;
    builder->capacity = 256U;
    return 1;
}

static void cg_builder_free(cg_wbuilder *builder)
{
    free(builder->data);
    builder->data = NULL;
    builder->length = 0;
    builder->capacity = 0;
}

static int cg_builder_reserve(cg_wbuilder *builder, size_t extra)
{
    size_t required;
    size_t next;
    wchar_t *grown;

    if (builder->length >= (size_t)CG_MAX_COMMAND_LINE ||
        extra > (size_t)CG_MAX_COMMAND_LINE - builder->length - 1U) {
        return 0;
    }
    required = builder->length + extra + 1U;
    if (required <= builder->capacity) {
        return 1;
    }
    next = builder->capacity;
    while (next < required) {
        if (next > (size_t)CG_MAX_COMMAND_LINE / 2U) {
            next = (size_t)CG_MAX_COMMAND_LINE + 1U;
            break;
        }
        next *= 2U;
    }
    if (next > (size_t)CG_MAX_COMMAND_LINE + 1U) {
        return 0;
    }
    grown = (wchar_t *)realloc(builder->data, next * sizeof(wchar_t));
    if (grown == NULL) {
        return 0;
    }
    builder->data = grown;
    builder->capacity = next;
    return 1;
}

static int cg_builder_char(cg_wbuilder *builder, wchar_t value)
{
    if (!cg_builder_reserve(builder, 1U)) {
        return 0;
    }
    builder->data[builder->length++] = value;
    builder->data[builder->length] = L'\0';
    return 1;
}

static int cg_builder_repeat(cg_wbuilder *builder, wchar_t value, size_t count)
{
    size_t i;
    if (!cg_builder_reserve(builder, count)) {
        return 0;
    }
    for (i = 0; i < count; i++) {
        builder->data[builder->length++] = value;
    }
    builder->data[builder->length] = L'\0';
    return 1;
}

static int cg_builder_text(cg_wbuilder *builder, const wchar_t *text)
{
    size_t length;
    if (text == NULL) {
        return 0;
    }
    length = wcslen(text);
    if (!cg_builder_reserve(builder, length)) {
        return 0;
    }
    memcpy(builder->data + builder->length, text, length * sizeof(wchar_t));
    builder->length += length;
    builder->data[builder->length] = L'\0';
    return 1;
}

static int cg_append_quoted(cg_wbuilder *builder, const wchar_t *argument)
{
    size_t i;
    size_t backslashes = 0;
    int needs_quotes = argument[0] == L'\0';

    for (i = 0; argument[i] != L'\0'; i++) {
        if (argument[i] == L' ' || argument[i] == L'\t' ||
            argument[i] == L'"') {
            needs_quotes = 1;
            break;
        }
    }
    if (!needs_quotes) {
        return cg_builder_text(builder, argument);
    }
    if (!cg_builder_char(builder, L'"')) {
        return 0;
    }
    for (i = 0; argument[i] != L'\0'; i++) {
        if (argument[i] == L'\\') {
            if (backslashes == SIZE_MAX) {
                return 0;
            }
            backslashes++;
        } else if (argument[i] == L'"') {
            if (backslashes > (SIZE_MAX - 1U) / 2U ||
                !cg_builder_repeat(builder, L'\\', backslashes * 2U + 1U) ||
                !cg_builder_char(builder, L'"')) {
                return 0;
            }
            backslashes = 0;
        } else {
            if (!cg_builder_repeat(builder, L'\\', backslashes) ||
                !cg_builder_char(builder, argument[i])) {
                return 0;
            }
            backslashes = 0;
        }
    }
    if (backslashes > SIZE_MAX / 2U ||
        !cg_builder_repeat(builder, L'\\', backslashes * 2U) ||
        !cg_builder_char(builder, L'"')) {
        return 0;
    }
    return 1;
}

static int cg_build_command_line(const cg_run_options *options,
                                 cg_wbuilder *builder)
{
    size_t i;

    if (!cg_builder_init(builder)) {
        return 0;
    }
    for (i = 0; i < options->argc; i++) {
        if (i != 0 && !cg_builder_char(builder, L' ')) {
            cg_builder_free(builder);
            return 0;
        }
        if (options->argv[i] == NULL ||
            !cg_append_quoted(builder, options->argv[i])) {
            cg_builder_free(builder);
            return 0;
        }
    }
    return builder->length > 0;
}

static void cg_capture_init(cg_capture_file *capture)
{
    capture->handle = INVALID_HANDLE_VALUE;
    capture->path[0] = L'\0';
    capture->active = 0;
}

static int cg_capture_create(cg_capture_file *capture)
{
    SECURITY_ATTRIBUTES attributes;
    wchar_t temp_directory[MAX_PATH];
    DWORD length;

    length = GetTempPathW((DWORD)_countof(temp_directory), temp_directory);
    if (length == 0 || length >= _countof(temp_directory)) {
        return 0;
    }
    if (GetTempFileNameW(temp_directory, L"cg", 0, capture->path) == 0) {
        return 0;
    }
    ZeroMemory(&attributes, sizeof(attributes));
    attributes.nLength = sizeof(attributes);
    /* The capture handle is retained by the parent for the readback.  It is
       not itself a child handle; the spawn path creates an explicit,
       child-only inheritable duplicate below. */
    attributes.bInheritHandle = FALSE;
    capture->handle = CreateFileW(
        capture->path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        &attributes,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_TEMPORARY,
        NULL);
    if (capture->handle == INVALID_HANDLE_VALUE) {
        DeleteFileW(capture->path);
        capture->path[0] = L'\0';
        return 0;
    }
    capture->active = 1;
    return 1;
}

static void cg_capture_close(cg_capture_file *capture)
{
    if (capture->handle != INVALID_HANDLE_VALUE) {
        CloseHandle(capture->handle);
        capture->handle = INVALID_HANDLE_VALUE;
    }
    if (capture->active && capture->path[0] != L'\0') {
        DeleteFileW(capture->path);
    }
    capture->path[0] = L'\0';
    capture->active = 0;
}

static int cg_read_capture(cg_capture_file *capture, char **data_out,
                           size_t *size_out, int *truncated_out,
                           DWORD *error_out)
{
    LARGE_INTEGER file_size;
    DWORD to_read;
    DWORD bytes_read = 0;
    char *data;
    LARGE_INTEGER file_offset;

    *data_out = NULL;
    *size_out = 0;
    *truncated_out = 0;
    *error_out = ERROR_SUCCESS;
    file_offset.QuadPart = 0;
    if (!SetFilePointerEx(capture->handle, file_offset, NULL, FILE_BEGIN) ||
        !GetFileSizeEx(capture->handle, &file_size)) {
        *error_out = GetLastError();
        return 0;
    }
    if (file_size.QuadPart < 0) {
        *error_out = ERROR_INVALID_DATA;
        return 0;
    }
    if ((unsigned long long)file_size.QuadPart > CG_OUTPUT_LIMIT) {
        to_read = CG_OUTPUT_LIMIT;
        *truncated_out = 1;
    } else {
        to_read = (DWORD)file_size.QuadPart;
    }
    data = (char *)malloc((size_t)to_read + 1U);
    if (data == NULL) {
        *error_out = ERROR_NOT_ENOUGH_MEMORY;
        return 0;
    }
    if (to_read > 0 &&
        (!ReadFile(capture->handle, data, to_read, &bytes_read, NULL) ||
         bytes_read != to_read)) {
        *error_out = GetLastError();
        free(data);
        return 0;
    }
    data[bytes_read] = '\0';
    *data_out = data;
    *size_out = bytes_read;
    return 1;
}

static void cg_normalize_newlines(char *data, size_t *size)
{
    size_t read_index = 0;
    size_t write_index = 0;

    while (read_index < *size) {
        if (data[read_index] == '\r') {
            data[write_index++] = '\n';
            read_index++;
            if (read_index < *size && data[read_index] == '\n') {
                read_index++;
            }
        } else {
            data[write_index++] = data[read_index++];
        }
    }
    *size = write_index;
    data[write_index] = '\0';
}

static int cg_wait_job_empty(HANDLE job, uint32_t timeout_ms, DWORD *error_out)
{
    uint64_t deadline = cg_now_ms() + timeout_ms;
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting;
    DWORD returned;

    for (;;) {
        ZeroMemory(&accounting, sizeof(accounting));
        if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                       &accounting, sizeof(accounting),
                                       &returned)) {
            if (error_out != NULL) {
                *error_out = GetLastError();
                if (*error_out == ERROR_SUCCESS) {
                    *error_out = ERROR_GEN_FAILURE;
                }
            }
            return 0;
        }
        if (accounting.ActiveProcesses == 0) {
            return 1;
        }
        if (cg_now_ms() >= deadline) {
            if (error_out != NULL) {
                *error_out = WAIT_TIMEOUT;
            }
            return 0;
        }
        Sleep(10);
    }
}

static int cg_terminate_job(HANDLE job, HANDLE process, cg_run_result *result,
                            DWORD exit_code)
{
    DWORD wait_result;
    DWORD cleanup_error = ERROR_SUCCESS;
    int process_exited = WaitForSingleObject(process, 0) == WAIT_OBJECT_0;

    if ((!process_exited || exit_code == CG_RESOURCE_TERMINATION_CODE) &&
        !TerminateJobObject(job, exit_code)) {
        DWORD error = GetLastError();
        if (error == ERROR_SUCCESS) {
            error = ERROR_GEN_FAILURE;
        }
        if (WaitForSingleObject(process, 0) != WAIT_OBJECT_0) {
            cg_set_error(result, error);
            result->cleanup_ok = 0;
            return 0;
        }
    }
    wait_result = WaitForSingleObject(process, CG_TIMEOUT_GRACE_MS);
    if (wait_result != WAIT_OBJECT_0 ||
        !cg_wait_job_empty(job, CG_TIMEOUT_GRACE_MS, &cleanup_error)) {
        if (wait_result == WAIT_FAILED) {
            cg_set_error(result, GetLastError());
        } else if (wait_result != WAIT_OBJECT_0) {
            cg_set_error(result, wait_result);
        } else if (cleanup_error != ERROR_SUCCESS) {
            cg_set_error(result, cleanup_error);
        }
        result->cleanup_ok = 0;
        return 0;
    }
    return 1;
}

static int cg_duplicate_child_handle(HANDLE source, HANDLE *child_handle_out,
                                     DWORD *error_out)
{
    HANDLE duplicate;

    *child_handle_out = source;
    if (source == NULL || source == INVALID_HANDLE_VALUE) {
        return 1;
    }
    duplicate = NULL;
    if (!DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(),
                         &duplicate, 0, TRUE, DUPLICATE_SAME_ACCESS)) {
        *error_out = GetLastError();
        return 0;
    }
    *child_handle_out = duplicate;
    return 1;
}

static void cg_close_child_handle(HANDLE source, HANDLE child_handle)
{
    if (child_handle != NULL && child_handle != INVALID_HANDLE_VALUE &&
        child_handle != source) {
        CloseHandle(child_handle);
    }
}

static int cg_windows_run_internal(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    cg_query_job_information_fn query_job_information)
{
    HANDLE job = NULL;
    HANDLE completion_port = NULL;
    HANDLE process = NULL;
    HANDLE thread = NULL;
    PROCESS_INFORMATION process_info;
    STARTUPINFOEXW startup_info;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    cg_wbuilder command_line;
    cg_capture_file stdout_capture;
    cg_capture_file stderr_capture;
    HANDLE stdin_handle;
    HANDLE stdout_handle;
    HANDLE stderr_handle;
    HANDLE child_stdin_handle;
    HANDLE child_stdout_handle;
    HANDLE child_stderr_handle;
    HANDLE inherited_handles[3];
    SIZE_T inherited_handle_count;
    SIZE_T attribute_list_size;
    LPPROC_THREAD_ATTRIBUTE_LIST attribute_list;
    int attribute_list_initialized;
    DWORD wait_result;
    DWORD exit_code = 0;
    DWORD last_error = ERROR_SUCCESS;
    uint64_t started_at = cg_now_ms();
    int process_started = 0;
    int job_assigned = 0;
    int terminate_attempted = 0;
    int output_error = 0;
    uint32_t resource_flags = 0;
    int memory_limit_enabled;
    int cpu_time_limit_enabled;
    int active_process_limit_enabled;
    int poll_completion_port;
    uint64_t cpu_time_limit_ticks = 0U;
    const cg_resource_limits *resource_limits = options->resource_limits;

    ZeroMemory(&process_info, sizeof(process_info));
    ZeroMemory(&startup_info, sizeof(startup_info));
    ZeroMemory(&limits, sizeof(limits));
    command_line.data = NULL;
    command_line.length = 0;
    command_line.capacity = 0;
    child_stdin_handle = NULL;
    child_stdout_handle = NULL;
    child_stderr_handle = NULL;
    stdin_handle = NULL;
    stdout_handle = NULL;
    stderr_handle = NULL;
    inherited_handle_count = 0;
    attribute_list_size = 0;
    attribute_list = NULL;
    attribute_list_initialized = 0;
    cg_capture_init(&stdout_capture);
    cg_capture_init(&stderr_capture);
    result->status = CG_STATUS_INTERNAL_ERROR;
    result->cleanup_ok = 1;
    memory_limit_enabled = cg_memory_limit_enabled(resource_limits);
    cpu_time_limit_enabled = cg_cpu_time_limit_enabled(resource_limits);
    active_process_limit_enabled =
        cg_active_process_limit_enabled(resource_limits);
    poll_completion_port = memory_limit_enabled || cpu_time_limit_enabled;

    job = CreateJobObjectW(NULL, NULL);
    if (job == NULL) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_CONTAINMENT_FAILED;
        goto cleanup;
    }
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (memory_limit_enabled) {
        if (resource_limits->memory_limit_bytes >
            (uint64_t)(SIZE_T)(~(SIZE_T)0)) {
            cg_set_error(result, ERROR_ARITHMETIC_OVERFLOW);
            result->status = CG_STATUS_USAGE_ERROR;
            goto cleanup;
        }
        limits.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY;
        limits.JobMemoryLimit = (SIZE_T)resource_limits->memory_limit_bytes;
    }
    if (cpu_time_limit_enabled) {
        if (!cg_cpu_time_limit_ticks(resource_limits->cpu_time_limit_ms,
                                     &cpu_time_limit_ticks)) {
            cg_set_error(result, ERROR_ARITHMETIC_OVERFLOW);
            result->status = CG_STATUS_USAGE_ERROR;
            goto cleanup;
        }
        limits.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_TIME;
        limits.BasicLimitInformation.PerJobUserTimeLimit.QuadPart =
            (LONGLONG)cpu_time_limit_ticks;
    }
    if (active_process_limit_enabled) {
        limits.BasicLimitInformation.LimitFlags |=
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
        limits.BasicLimitInformation.ActiveProcessLimit =
            (DWORD)resource_limits->active_process_limit;
    }
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                 &limits, sizeof(limits))) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_CONTAINMENT_FAILED;
        goto cleanup;
    }
    if ((memory_limit_enabled || active_process_limit_enabled) &&
        !cg_create_resource_monitor(job, memory_limit_enabled,
                                    memory_limit_enabled
                                        ? resource_limits->memory_limit_bytes
                                        : 0U,
                                    &completion_port, &last_error)) {
        cg_set_error(result, last_error);
        result->status = CG_STATUS_CONTAINMENT_FAILED;
        goto cleanup;
    }
    if (options->capture_output &&
        (!cg_capture_create(&stdout_capture) ||
         !cg_capture_create(&stderr_capture))) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_INTERNAL_ERROR;
        goto cleanup;
    }
    if (!cg_build_command_line(options, &command_line)) {
        cg_set_error(result, ERROR_BUFFER_OVERFLOW);
        result->status = CG_STATUS_USAGE_ERROR;
        goto cleanup;
    }

    stdin_handle = GetStdHandle(STD_INPUT_HANDLE);
    stdout_handle = options->capture_output ? stdout_capture.handle
                                            : GetStdHandle(STD_OUTPUT_HANDLE);
    stderr_handle = options->capture_output ? stderr_capture.handle
                                            : GetStdHandle(STD_ERROR_HANDLE);
    if (!cg_duplicate_child_handle(stdin_handle, &child_stdin_handle,
                                   &last_error) ||
        !cg_duplicate_child_handle(stdout_handle, &child_stdout_handle,
                                   &last_error) ||
        !cg_duplicate_child_handle(stderr_handle, &child_stderr_handle,
                                   &last_error)) {
        cg_set_error(result, last_error);
        result->status = CG_STATUS_START_FAILED;
        goto cleanup;
    }
    startup_info.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup_info.StartupInfo.hStdInput = child_stdin_handle;
    startup_info.StartupInfo.hStdOutput = child_stdout_handle;
    startup_info.StartupInfo.hStdError = child_stderr_handle;
    if (child_stdin_handle != NULL &&
        child_stdin_handle != INVALID_HANDLE_VALUE) {
        inherited_handles[inherited_handle_count++] = child_stdin_handle;
    }
    if (child_stdout_handle != NULL &&
        child_stdout_handle != INVALID_HANDLE_VALUE) {
        inherited_handles[inherited_handle_count++] = child_stdout_handle;
    }
    if (child_stderr_handle != NULL &&
        child_stderr_handle != INVALID_HANDLE_VALUE) {
        inherited_handles[inherited_handle_count++] = child_stderr_handle;
    }

    if (inherited_handle_count > 0) {
        attribute_list = NULL;
        if (InitializeProcThreadAttributeList(NULL, 1, 0,
                                               &attribute_list_size) ||
            GetLastError() != ERROR_INSUFFICIENT_BUFFER ||
            attribute_list_size == 0) {
            cg_set_error(result, GetLastError());
            result->status = CG_STATUS_START_FAILED;
            goto cleanup;
        }
        attribute_list = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(
            GetProcessHeap(), 0, attribute_list_size);
        if (attribute_list == NULL) {
            cg_set_error(result, ERROR_NOT_ENOUGH_MEMORY);
            result->status = CG_STATUS_START_FAILED;
            goto cleanup;
        }
        if (!InitializeProcThreadAttributeList(attribute_list, 1, 0,
                                               &attribute_list_size)) {
            cg_set_error(result, GetLastError());
            result->status = CG_STATUS_START_FAILED;
            goto cleanup;
        }
        attribute_list_initialized = 1;
        if (!UpdateProcThreadAttribute(
                attribute_list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                inherited_handles,
                inherited_handle_count * sizeof(inherited_handles[0]), NULL,
                NULL)) {
            cg_set_error(result, GetLastError());
            result->status = CG_STATUS_START_FAILED;
            goto cleanup;
        }
        startup_info.StartupInfo.cb = sizeof(startup_info);
        startup_info.lpAttributeList = attribute_list;
    } else {
        startup_info.StartupInfo.cb = sizeof(startup_info.StartupInfo);
    }

    if (!CreateProcessW(
            NULL, command_line.data, NULL, NULL,
            inherited_handle_count > 0,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT |
                (inherited_handle_count > 0 ? EXTENDED_STARTUPINFO_PRESENT : 0),
            NULL, NULL, &startup_info.StartupInfo, &process_info)) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_START_FAILED;
        goto cleanup;
    }
    process_started = 1;
    process = process_info.hProcess;
    thread = process_info.hThread;
    result->process_id = process_info.dwProcessId;

    if (!AssignProcessToJobObject(job, process)) {
        last_error = GetLastError();
        result->status = CG_STATUS_CONTAINMENT_FAILED;
        cg_set_error(result, last_error);
        result->cleanup_ok = 0;
        TerminateProcess(process, 125U);
        WaitForSingleObject(process, CG_TIMEOUT_GRACE_MS);
        goto cleanup;
    }
    job_assigned = 1;
    if (ResumeThread(thread) == (DWORD)-1) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_INTERNAL_ERROR;
        result->cleanup_ok = 0;
        TerminateJobObject(job, 125U);
        WaitForSingleObject(process, CG_TIMEOUT_GRACE_MS);
        goto cleanup;
    }

    wait_result = cg_wait_for_process(
        process, completion_port, job, cpu_time_limit_enabled
            ? cpu_time_limit_ticks : 0U,
        poll_completion_port, (DWORD)options->timeout_ms, &resource_flags,
        &last_error);
    if (wait_result != WAIT_FAILED && completion_port != NULL &&
        !cg_drain_resource_notifications(completion_port, &resource_flags,
                                         &last_error)) {
        wait_result = WAIT_FAILED;
    }
    result->resource_limit_hit = resource_flags != 0U;
    result->resource_limit_kind = cg_resource_limit_kind(resource_flags);
    if (wait_result == CG_WAIT_RESOURCE ||
        (wait_result == WAIT_TIMEOUT && resource_flags != 0U)) {
        result->status = CG_STATUS_RESOURCE_LIMIT;
        terminate_attempted = 1;
        (void)cg_terminate_job(job, process, result,
                               CG_RESOURCE_TERMINATION_CODE);
    } else if (wait_result == WAIT_TIMEOUT &&
               WaitForSingleObject(process, 0) != WAIT_OBJECT_0) {
        result->timed_out = 1;
        result->status = CG_STATUS_TIMEOUT;
        terminate_attempted = 1;
        (void)cg_terminate_job(job, process, result, 124U);
    } else if (wait_result == WAIT_OBJECT_0) {
        if (resource_flags != 0U) {
            result->status = CG_STATUS_RESOURCE_LIMIT;
            terminate_attempted = 1;
            (void)cg_terminate_job(job, process, result,
                                   CG_RESOURCE_TERMINATION_CODE);
        } else {
            result->status = CG_STATUS_EXITED;
        }
    } else {
        cg_set_error(result, last_error);
        result->status = CG_STATUS_INTERNAL_ERROR;
        terminate_attempted = 1;
        if (job_assigned) {
            (void)cg_terminate_job(job, process, result, 125U);
        }
    }

    if (result->status == CG_STATUS_EXITED &&
        (!GetExitCodeProcess(process, &exit_code))) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_INTERNAL_ERROR;
    } else if (result->status == CG_STATUS_EXITED) {
        result->has_exit_code = 1;
        result->exit_code = (uint32_t)exit_code;
    }

    if (process_started && process != NULL &&
        (result->status == CG_STATUS_EXITED ||
         (result->status == CG_STATUS_RESOURCE_LIMIT && result->cleanup_ok) ||
         (result->status == CG_STATUS_TIMEOUT && result->cleanup_ok))) {
        /* The process handle remains valid until the cleanup block below. */
        cg_collect_process_metrics(process, &result->metrics);
    }

    if (options->capture_output) {
        DWORD output_error_code;
        if (!cg_read_capture(&stdout_capture, &result->stdout_utf8,
                             &result->stdout_size, &result->output_truncated,
                             &output_error_code)) {
            output_error = 1;
            last_error = output_error_code;
        }
        {
            int stderr_truncated;
            if (!cg_read_capture(&stderr_capture, &result->stderr_utf8,
                                 &result->stderr_size, &stderr_truncated,
                                 &output_error_code)) {
                output_error = 1;
                last_error = output_error_code;
            }
            if (stderr_truncated) {
                result->output_truncated = 1;
            }
        }
        if (result->stdout_utf8 != NULL) {
            cg_normalize_newlines(result->stdout_utf8, &result->stdout_size);
        }
        if (result->stderr_utf8 != NULL) {
            cg_normalize_newlines(result->stderr_utf8, &result->stderr_size);
        }
        if (output_error) {
            cg_set_error(result, last_error);
            if (result->status == CG_STATUS_EXITED) {
                result->status = CG_STATUS_INTERNAL_ERROR;
            }
        }
    }

cleanup:
    result->duration_ms = cg_now_ms() - started_at;
    if (process_started && process != NULL &&
        result->status != CG_STATUS_TIMEOUT && terminate_attempted == 0) {
        /* A normal exit leaves no active job members. */
        if (job_assigned && !cg_wait_job_empty(job, 100U, NULL)) {
            result->cleanup_ok = 0;
        }
    }
    if (process != NULL && result->status == CG_STATUS_TIMEOUT &&
        !result->cleanup_ok) {
        /* The close-on-close job flag is the final kernel backstop. */
        result->cleanup_ok = 0;
    }
    if (result->status == CG_STATUS_RESOURCE_LIMIT && !result->cleanup_ok) {
        result->status = CG_STATUS_CONTAINMENT_FAILED;
    }
    if (job_metrics != NULL && job_assigned && job != NULL) {
        cg_collect_job_metrics(job, job_metrics, query_job_information);
    }
    if (attribute_list_initialized) {
        DeleteProcThreadAttributeList(attribute_list);
    }
    if (attribute_list != NULL) {
        HeapFree(GetProcessHeap(), 0, attribute_list);
    }
    cg_close_child_handle(stdin_handle, child_stdin_handle);
    cg_close_child_handle(stdout_handle, child_stdout_handle);
    cg_close_child_handle(stderr_handle, child_stderr_handle);
    if (thread != NULL) {
        CloseHandle(thread);
    }
    if (process != NULL) {
        CloseHandle(process);
    }
    cg_capture_close(&stdout_capture);
    cg_capture_close(&stderr_capture);
    if (completion_port != NULL) {
        CloseHandle(completion_port);
    }
    if (job != NULL) {
        CloseHandle(job);
    }
    cg_builder_free(&command_line);
    return 0;
}

int cg_windows_run(const cg_run_options *options, cg_run_result *result,
                   cg_job_metrics *job_metrics)
{
    return cg_windows_run_internal(options, result, job_metrics,
                                   cg_query_job_information);
}

#ifdef COREGUARD_TEST_HOOKS
int cg_windows_run_with_job_metrics_query_hook(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    cg_query_job_information_fn query_job_information)
{
    if (query_job_information == NULL) {
        return -1;
    }
    return cg_windows_run_internal(options, result, job_metrics,
                                   query_job_information);
}
#endif
