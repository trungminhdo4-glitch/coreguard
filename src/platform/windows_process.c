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
#define CG_WAIT_OUTPUT 0x10001U
#define CG_RESOURCE_FLAG_MEMORY 0x1U
#define CG_RESOURCE_FLAG_CPU_TIME 0x2U
#define CG_RESOURCE_FLAG_ACTIVE_PROCESSES 0x4U
#define CG_CAPTURE_READ_SIZE 8192U
#define CG_CAPTURE_DRAIN_GRACE_MS 100U
#define CG_CAPTURE_CANCEL_GRACE_MS 5000U
#define CG_CAPTURE_CANCEL_POLL_MS 20U
#define CG_CAPTURE_QUARANTINE_LIMIT 16L
#define CG_CAPTURE_REAP_LIMIT 4U
#define CG_FILETIME_UNIX_EPOCH_100NS UINT64_C(116444736000000000)
#define CG_100NS_PER_MS UINT64_C(10000)

typedef struct cg_wbuilder {
    wchar_t *data;
    size_t length;
    size_t capacity;
} cg_wbuilder;

typedef struct cg_capture_pipe {
    HANDLE read_handle;
    HANDLE write_handle;
    HANDLE thread;
    HANDLE stop_event;
    HANDLE failure_event;
    cg_read_file_fn read_file;
    char *data;
    size_t size;
    volatile LONG read_error;
    volatile LONG truncated;
    int thread_joined;
    int slot_reserved;
    int quarantined;
    struct cg_capture_pipe *next_quarantined;
} cg_capture_pipe;

static SRWLOCK cg_capture_quarantine_lock = SRWLOCK_INIT;
static cg_capture_pipe *cg_quarantined_captures;
static cg_capture_pipe *cg_quarantined_captures_tail;
static volatile LONG cg_capture_slots_used;
static volatile LONG cg_quarantined_capture_count;

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
                                  int poll_completion_port,
                                  HANDLE output_failure_event,
                                  DWORD timeout_ms,
                                  uint32_t *resource_flags,
                                  DWORD *error_out)
{
    uint64_t deadline = cg_now_ms() + (uint64_t)timeout_ms;

    if (!poll_completion_port && cpu_time_limit_ticks == 0U) {
        DWORD wait_result;
        if (output_failure_event == NULL) {
            wait_result = WaitForSingleObject(process, timeout_ms);
        } else {
            HANDLE wait_handles[2];
            wait_handles[0] = process;
            wait_handles[1] = output_failure_event;
            wait_result = WaitForMultipleObjects(2, wait_handles, FALSE,
                                                 timeout_ms);
            if (wait_result == WAIT_OBJECT_0 + 1U) {
                return CG_WAIT_OUTPUT;
            }
        }
        if (wait_result == WAIT_FAILED) {
            *error_out = GetLastError();
        }
        return wait_result;
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
        if (output_failure_event != NULL) {
            wait_result = WaitForSingleObject(output_failure_event, 0);
            if (wait_result == WAIT_FAILED) {
                *error_out = GetLastError();
                return WAIT_FAILED;
            }
            if (wait_result == WAIT_OBJECT_0) {
                return CG_WAIT_OUTPUT;
            }
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
            if (output_failure_event != NULL) {
                DWORD output_wait =
                    WaitForSingleObject(output_failure_event, 0);
                if (output_wait == WAIT_FAILED) {
                    *error_out = GetLastError();
                    return WAIT_FAILED;
                }
                if (output_wait == WAIT_OBJECT_0) {
                    return CG_WAIT_OUTPUT;
                }
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
            HANDLE wait_handles[3];
            DWORD handle_count = 2U;
            wait_handles[0] = process;
            wait_handles[1] = job;
            if (output_failure_event != NULL) {
                wait_handles[handle_count++] = output_failure_event;
            }
            wait_result = WaitForMultipleObjects(handle_count, wait_handles,
                                                 FALSE, wait_ms);
            if (wait_result == WAIT_OBJECT_0 + 1U) {
                *resource_flags |= CG_RESOURCE_FLAG_CPU_TIME;
                return CG_WAIT_RESOURCE;
            }
            if (output_failure_event != NULL &&
                wait_result == WAIT_OBJECT_0 + 2U) {
                return CG_WAIT_OUTPUT;
            }
        } else {
            if (output_failure_event == NULL) {
                wait_result = WaitForSingleObject(process, wait_ms);
            } else {
                HANDLE wait_handles[2];
                wait_handles[0] = process;
                wait_handles[1] = output_failure_event;
                wait_result = WaitForMultipleObjects(2, wait_handles, FALSE,
                                                     wait_ms);
                if (wait_result == WAIT_OBJECT_0 + 1U) {
                    return CG_WAIT_OUTPUT;
                }
            }
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

static void cg_capture_init(cg_capture_pipe *capture)
{
    capture->read_handle = INVALID_HANDLE_VALUE;
    capture->write_handle = INVALID_HANDLE_VALUE;
    capture->thread = NULL;
    capture->stop_event = NULL;
    capture->failure_event = NULL;
    capture->read_file = NULL;
    capture->data = NULL;
    capture->size = 0U;
    capture->read_error = (LONG)ERROR_SUCCESS;
    capture->truncated = 0;
    capture->thread_joined = 0;
    capture->slot_reserved = 0;
    capture->quarantined = 0;
    capture->next_quarantined = NULL;
}

static int cg_capture_reserve_slots(cg_capture_pipe *stdout_capture,
                                    cg_capture_pipe *stderr_capture)
{
    LONG current;

    if (stdout_capture == NULL || stderr_capture == NULL) {
        return 0;
    }
    for (;;) {
        current = InterlockedCompareExchange(&cg_capture_slots_used, 0, 0);
        if (current > CG_CAPTURE_QUARANTINE_LIMIT - 2L) {
            return 0;
        }
        if (InterlockedCompareExchange(&cg_capture_slots_used, current + 2L,
                                       current) == current) {
            stdout_capture->slot_reserved = 1;
            stderr_capture->slot_reserved = 1;
            return 1;
        }
    }
}

static void cg_capture_release_slot(cg_capture_pipe *capture)
{
    if (capture->quarantined) {
        capture->quarantined = 0;
        (void)InterlockedDecrement(&cg_quarantined_capture_count);
    }
    if (capture->slot_reserved) {
        capture->slot_reserved = 0;
        (void)InterlockedDecrement(&cg_capture_slots_used);
    }
}

static void cg_capture_destroy(cg_capture_pipe *capture)
{
    if (capture == NULL) {
        return;
    }
    if (capture->thread != NULL && !capture->thread_joined) {
        return;
    }
    if (capture->thread != NULL) {
        CloseHandle(capture->thread);
    }
    if (capture->read_handle != INVALID_HANDLE_VALUE) {
        CloseHandle(capture->read_handle);
    }
    if (capture->write_handle != INVALID_HANDLE_VALUE) {
        CloseHandle(capture->write_handle);
    }
    if (capture->stop_event != NULL) {
        CloseHandle(capture->stop_event);
    }
    if (capture->failure_event != NULL) {
        CloseHandle(capture->failure_event);
    }
    free(capture->data);
    free(capture);
}

static int cg_capture_create(cg_capture_pipe **capture_out, DWORD *error_out)
{
    cg_capture_pipe *capture;
    SECURITY_ATTRIBUTES attributes;

    *capture_out = NULL;
    capture = (cg_capture_pipe *)calloc(1U, sizeof(*capture));
    if (capture == NULL) {
        *error_out = ERROR_NOT_ENOUGH_MEMORY;
        return 0;
    }
    cg_capture_init(capture);
    capture->data = (char *)malloc((size_t)CG_OUTPUT_LIMIT + 1U);
    if (capture->data == NULL) {
        *error_out = ERROR_NOT_ENOUGH_MEMORY;
        cg_capture_destroy(capture);
        return 0;
    }
    capture->data[0] = '\0';
    capture->stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (capture->stop_event == NULL) {
        *error_out = GetLastError();
        cg_capture_destroy(capture);
        return 0;
    }
    ZeroMemory(&attributes, sizeof(attributes));
    attributes.nLength = sizeof(attributes);
    attributes.bInheritHandle = FALSE;
    if (!CreatePipe(&capture->read_handle, &capture->write_handle,
                    &attributes, 0)) {
        *error_out = GetLastError();
        cg_capture_destroy(capture);
        return 0;
    }
    *capture_out = capture;
    return 1;
}

static int cg_capture_stop_requested(cg_capture_pipe *capture,
                                     DWORD *error_out)
{
    DWORD wait_result = WaitForSingleObject(capture->stop_event, 0);

    if (wait_result == WAIT_OBJECT_0) {
        return 1;
    }
    if (wait_result == WAIT_FAILED) {
        *error_out = GetLastError();
        return -1;
    }
    return 0;
}

static void cg_capture_signal_failure(cg_capture_pipe *capture, DWORD error)
{
    (void)InterlockedExchange(&capture->read_error, (LONG)error);
    if (capture->failure_event != NULL) {
        (void)SetEvent(capture->failure_event);
    }
}

static DWORD WINAPI cg_capture_reader(LPVOID parameter)
{
    cg_capture_pipe *capture;
    char chunk[CG_CAPTURE_READ_SIZE];

    if (parameter == NULL) {
        return ERROR_INVALID_PARAMETER;
    }
    capture = (cg_capture_pipe *)parameter;
    if (capture->data == NULL ||
        capture->read_handle == INVALID_HANDLE_VALUE ||
        capture->stop_event == NULL ||
        capture->failure_event == NULL || capture->read_file == NULL) {
        cg_capture_signal_failure(capture, ERROR_INVALID_HANDLE);
        return ERROR_INVALID_HANDLE;
    }
    for (;;) {
        DWORD bytes_read = 0U;
        size_t copy_size;
        size_t remaining;
        DWORD error = ERROR_SUCCESS;
        DWORD stop_error = ERROR_SUCCESS;
        int stop_requested = cg_capture_stop_requested(capture, &error);

        if (stop_requested > 0) {
            break;
        }
        if (stop_requested < 0) {
            cg_capture_signal_failure(capture, error);
            break;
        }

        if (!capture->read_file(capture->read_handle, chunk,
                                (DWORD)sizeof(chunk), &bytes_read, NULL)) {
            error = GetLastError();
            if (error == ERROR_BROKEN_PIPE) {
                break;
            }
            stop_requested = cg_capture_stop_requested(capture, &stop_error);
            if (error == ERROR_OPERATION_ABORTED && stop_requested > 0) {
                break;
            }
            if (stop_requested < 0) {
                error = stop_error;
            }
            cg_capture_signal_failure(capture, error);
            break;
        }
        if (bytes_read == 0U) {
            break;
        }
        if (bytes_read > (DWORD)sizeof(chunk)) {
            cg_capture_signal_failure(capture, ERROR_INVALID_DATA);
            break;
        }
        remaining = capture->size < (size_t)CG_OUTPUT_LIMIT
                        ? (size_t)CG_OUTPUT_LIMIT - capture->size
                        : 0U;
        copy_size = (size_t)bytes_read;
        if (copy_size > remaining) {
            copy_size = remaining;
        }
        if (copy_size > 0U) {
            memcpy(capture->data + capture->size, chunk, copy_size);
            capture->size += copy_size;
        }
        if (copy_size < (size_t)bytes_read) {
            (void)InterlockedExchange(&capture->truncated, 1);
        }
    }
    capture->data[capture->size] = '\0';
    return 0U;
}

static int cg_capture_start(cg_capture_pipe *capture, HANDLE failure_event,
                            cg_read_file_fn read_file, DWORD *error_out)
{
    capture->read_file = read_file;
    if (!DuplicateHandle(GetCurrentProcess(), failure_event,
                         GetCurrentProcess(), &capture->failure_event, 0,
                         FALSE, DUPLICATE_SAME_ACCESS)) {
        *error_out = GetLastError();
        return 0;
    }
    capture->thread = CreateThread(NULL, 0, cg_capture_reader, capture, 0,
                                   NULL);
    if (capture->thread == NULL) {
        *error_out = GetLastError();
        CloseHandle(capture->failure_event);
        capture->failure_event = NULL;
        return 0;
    }
    return 1;
}

static int cg_capture_close_write(cg_capture_pipe *capture, DWORD *error_out)
{
    if (capture->write_handle == INVALID_HANDLE_VALUE) {
        return 1;
    }
    if (!CloseHandle(capture->write_handle)) {
        *error_out = GetLastError();
        return 0;
    }
    capture->write_handle = INVALID_HANDLE_VALUE;
    return 1;
}

static int cg_capture_join(cg_capture_pipe *capture,
                           cg_wait_single_fn wait_single,
                           cg_cancel_synchronous_io_fn cancel_io,
                           DWORD cancel_grace_ms,
                           DWORD *error_out)
{
    DWORD wait_result;
    DWORD operation_error = ERROR_SUCCESS;
    uint64_t deadline;

    *error_out = ERROR_SUCCESS;
    if (capture == NULL) {
        return 1;
    }
    if (capture->thread == NULL) {
        capture->thread_joined = 1;
        return 1;
    }
    wait_result = wait_single(capture->thread, CG_CAPTURE_DRAIN_GRACE_MS);
    if (wait_result == WAIT_OBJECT_0) {
        capture->thread_joined = 1;
        return 1;
    }
    if (wait_result == WAIT_FAILED) {
        operation_error = GetLastError();
        if (operation_error == ERROR_SUCCESS) {
            operation_error = ERROR_GEN_FAILURE;
        }
    } else if (wait_result != WAIT_TIMEOUT) {
        operation_error = ERROR_GEN_FAILURE;
    }
    deadline = cg_now_ms() + (uint64_t)cancel_grace_ms;
    for (;;) {
        uint64_t now;
        uint64_t remaining;
        DWORD wait_ms;

        if (!SetEvent(capture->stop_event) && operation_error == ERROR_SUCCESS) {
            operation_error = GetLastError();
        }
        if (!cancel_io(capture->thread)) {
            DWORD cancel_error = GetLastError();
            if (cancel_error != ERROR_NOT_FOUND &&
                operation_error == ERROR_SUCCESS) {
                operation_error = cancel_error;
            }
        }
        now = cg_now_ms();
        remaining = now < deadline ? deadline - now : 0U;
        wait_ms = remaining > CG_CAPTURE_CANCEL_POLL_MS
                      ? CG_CAPTURE_CANCEL_POLL_MS
                      : (DWORD)remaining;
        wait_result = wait_single(capture->thread, wait_ms);
        if (wait_result == WAIT_OBJECT_0) {
            capture->thread_joined = 1;
            *error_out = operation_error;
            return 1;
        }
        if (wait_result == WAIT_FAILED) {
            *error_out = GetLastError();
            if (*error_out == ERROR_SUCCESS) {
                *error_out = ERROR_GEN_FAILURE;
            }
            return 0;
        }
        if (wait_result != WAIT_TIMEOUT) {
            *error_out = ERROR_GEN_FAILURE;
            return 0;
        }
        if (cg_now_ms() >= deadline) {
            *error_out = operation_error == ERROR_SUCCESS ? WAIT_TIMEOUT
                                                           : operation_error;
            return 0;
        }
    }
}

static int cg_capture_take(cg_capture_pipe *capture, char **data_out,
                           size_t *size_out, DWORD *error_out)
{
    LONG read_error = InterlockedCompareExchange(&capture->read_error, 0, 0);
    int succeeded = read_error == (LONG)ERROR_SUCCESS;

    if (capture->thread != NULL && !capture->thread_joined) {
        *error_out = ERROR_BUSY;
        return 0;
    }
    if (!succeeded) {
        *error_out = (DWORD)read_error;
    }
    *data_out = capture->data;
    *size_out = capture->size;
    capture->data = NULL;
    capture->size = 0U;
    return succeeded;
}

static int cg_capture_close(cg_capture_pipe *capture)
{
    if (capture == NULL) {
        return 1;
    }
    if (capture->thread != NULL && !capture->thread_joined) {
        return 0;
    }
    if (capture->thread != NULL) {
        CloseHandle(capture->thread);
        capture->thread = NULL;
    }
    if (capture->read_handle != INVALID_HANDLE_VALUE) {
        CloseHandle(capture->read_handle);
        capture->read_handle = INVALID_HANDLE_VALUE;
    }
    if (capture->write_handle != INVALID_HANDLE_VALUE) {
        CloseHandle(capture->write_handle);
        capture->write_handle = INVALID_HANDLE_VALUE;
    }
    if (capture->stop_event != NULL) {
        CloseHandle(capture->stop_event);
        capture->stop_event = NULL;
    }
    if (capture->failure_event != NULL) {
        CloseHandle(capture->failure_event);
        capture->failure_event = NULL;
    }
    free(capture->data);
    capture->data = NULL;
    capture->size = 0U;
    cg_capture_release_slot(capture);
    free(capture);
    return 1;
}

static void cg_capture_quarantine(cg_capture_pipe *capture)
{
    if (capture == NULL) {
        return;
    }
    AcquireSRWLockExclusive(&cg_capture_quarantine_lock);
    capture->next_quarantined = NULL;
    if (cg_quarantined_captures_tail != NULL) {
        cg_quarantined_captures_tail->next_quarantined = capture;
    } else {
        cg_quarantined_captures = capture;
    }
    cg_quarantined_captures_tail = capture;
    if (!capture->quarantined) {
        capture->quarantined = 1;
        (void)InterlockedIncrement(&cg_quarantined_capture_count);
    }
    ReleaseSRWLockExclusive(&cg_capture_quarantine_lock);
}

static void cg_capture_reap_quarantined(void)
{
    cg_capture_pipe *deferred = NULL;
    cg_capture_pipe *deferred_tail = NULL;
    unsigned int reaped;

    for (reaped = 0U; reaped < CG_CAPTURE_REAP_LIMIT; reaped++) {
        cg_capture_pipe *capture;
        DWORD wait_result;

        AcquireSRWLockExclusive(&cg_capture_quarantine_lock);
        capture = cg_quarantined_captures;
        if (capture != NULL) {
            cg_quarantined_captures = capture->next_quarantined;
            if (cg_quarantined_captures == NULL) {
                cg_quarantined_captures_tail = NULL;
            }
        }
        ReleaseSRWLockExclusive(&cg_capture_quarantine_lock);
        if (capture == NULL) {
            break;
        }
        capture->next_quarantined = NULL;
        wait_result = capture->thread == NULL
                          ? WAIT_OBJECT_0
                          : WaitForSingleObject(capture->thread, 0);
        if (wait_result == WAIT_OBJECT_0) {
            capture->thread_joined = 1;
            (void)cg_capture_close(capture);
        } else {
            if (deferred_tail != NULL) {
                deferred_tail->next_quarantined = capture;
            } else {
                deferred = capture;
            }
            deferred_tail = capture;
        }
    }
    if (deferred != NULL) {
        AcquireSRWLockExclusive(&cg_capture_quarantine_lock);
        if (cg_quarantined_captures_tail != NULL) {
            cg_quarantined_captures_tail->next_quarantined = deferred;
        } else {
            cg_quarantined_captures = deferred;
        }
        cg_quarantined_captures_tail = deferred_tail;
        ReleaseSRWLockExclusive(&cg_capture_quarantine_lock);
    }
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

static int cg_terminate_unassigned_process(HANDLE process,
                                            cg_run_result *result)
{
    DWORD wait_result;

    if (!TerminateProcess(process, 125U)) {
        DWORD error = GetLastError();
        wait_result = WaitForSingleObject(process, 0);
        if (wait_result != WAIT_OBJECT_0) {
            cg_set_error(result, error == ERROR_SUCCESS ? ERROR_GEN_FAILURE
                                                        : error);
            result->cleanup_ok = 0;
            return 0;
        }
        return 1;
    }
    wait_result = WaitForSingleObject(process, CG_TIMEOUT_GRACE_MS);
    if (wait_result != WAIT_OBJECT_0) {
        cg_set_error(result, wait_result == WAIT_FAILED ? GetLastError()
                                                        : WAIT_TIMEOUT);
        result->cleanup_ok = 0;
        return 0;
    }
    return 1;
}

static cg_windows_test_hooks cg_default_windows_hooks(void)
{
    cg_windows_test_hooks hooks;

    hooks.query_job_information = cg_query_job_information;
    hooks.read_file = ReadFile;
    hooks.assign_process_to_job = AssignProcessToJobObject;
    hooks.wait_capture_thread = WaitForSingleObject;
    hooks.cancel_synchronous_io = CancelSynchronousIo;
    hooks.capture_cancel_grace_ms = CG_CAPTURE_CANCEL_GRACE_MS;
    return hooks;
}

static int cg_windows_run_internal(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    const cg_windows_test_hooks *hooks)
{
    HANDLE job = NULL;
    HANDLE completion_port = NULL;
    HANDLE output_failure_event = NULL;
    HANDLE process = NULL;
    HANDLE thread = NULL;
    PROCESS_INFORMATION process_info;
    STARTUPINFOEXW startup_info;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    cg_wbuilder command_line;
    cg_capture_pipe *stdout_capture = NULL;
    cg_capture_pipe *stderr_capture = NULL;
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
    int capture_output_available = 0;
    uint32_t resource_flags = 0;
    int memory_limit_enabled;
    int cpu_time_limit_enabled;
    int active_process_limit_enabled;
    int poll_completion_port;
    uint64_t cpu_time_limit_ticks = 0U;
    const cg_resource_limits *resource_limits = options->resource_limits;

    cg_capture_reap_quarantined();
    if (hooks == NULL || hooks->query_job_information == NULL ||
        hooks->read_file == NULL || hooks->assign_process_to_job == NULL ||
        hooks->wait_capture_thread == NULL ||
        hooks->cancel_synchronous_io == NULL ||
        hooks->capture_cancel_grace_ms == 0U) {
        cg_set_error(result, ERROR_INVALID_PARAMETER);
        result->status = CG_STATUS_INTERNAL_ERROR;
        return 0;
    }

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
    if (options->capture_output) {
        if (!cg_capture_create(&stdout_capture, &last_error) ||
            !cg_capture_create(&stderr_capture, &last_error)) {
            cg_set_error(result, last_error);
            result->status = CG_STATUS_INTERNAL_ERROR;
            goto cleanup;
        }
        output_failure_event = CreateEventW(NULL, TRUE, FALSE, NULL);
        if (output_failure_event == NULL) {
            cg_set_error(result, GetLastError());
            result->status = CG_STATUS_INTERNAL_ERROR;
            goto cleanup;
        }
        if (!cg_capture_reserve_slots(stdout_capture, stderr_capture)) {
            cg_set_error(result, ERROR_NOT_ENOUGH_QUOTA);
            result->status = CG_STATUS_INTERNAL_ERROR;
            goto cleanup;
        }
    }
    if (!cg_build_command_line(options, &command_line)) {
        cg_set_error(result, ERROR_BUFFER_OVERFLOW);
        result->status = CG_STATUS_USAGE_ERROR;
        goto cleanup;
    }

    stdin_handle = GetStdHandle(STD_INPUT_HANDLE);
    stdout_handle = options->capture_output ? stdout_capture->write_handle
                                             : GetStdHandle(STD_OUTPUT_HANDLE);
    stderr_handle = options->capture_output ? stderr_capture->write_handle
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
    if (options->capture_output) {
        int write_close_ok = 1;

        if (!cg_capture_close_write(stdout_capture, &last_error)) {
            write_close_ok = 0;
        }
        if (!cg_capture_close_write(stderr_capture, &last_error)) {
            write_close_ok = 0;
        }
        if (!write_close_ok) {
            cg_set_error(result, last_error);
            result->status = CG_STATUS_START_FAILED;
            goto cleanup;
        }
        stdout_handle = NULL;
        stderr_handle = NULL;
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
    cg_close_child_handle(stdin_handle, child_stdin_handle);
    child_stdin_handle = NULL;
    cg_close_child_handle(stdout_handle, child_stdout_handle);
    child_stdout_handle = NULL;
    cg_close_child_handle(stderr_handle, child_stderr_handle);
    child_stderr_handle = NULL;
    process_started = 1;
    process = process_info.hProcess;
    thread = process_info.hThread;
    result->process_id = process_info.dwProcessId;

    if (!hooks->assign_process_to_job(job, process)) {
        last_error = GetLastError();
        result->status = CG_STATUS_CONTAINMENT_FAILED;
        cg_set_error(result, last_error);
        (void)cg_terminate_unassigned_process(process, result);
        goto cleanup;
    }
    job_assigned = 1;
    if (options->capture_output) {
        if (!cg_capture_start(stdout_capture, output_failure_event,
                              hooks->read_file, &last_error) ||
            !cg_capture_start(stderr_capture, output_failure_event,
                              hooks->read_file, &last_error)) {
            cg_set_error(result, last_error);
            result->status = CG_STATUS_INTERNAL_ERROR;
            result->cleanup_ok = 0;
            terminate_attempted = 1;
            (void)cg_terminate_job(job, process, result, 125U);
            goto cleanup;
        }
    }
    if (ResumeThread(thread) == (DWORD)-1) {
        cg_set_error(result, GetLastError());
        result->status = CG_STATUS_INTERNAL_ERROR;
        result->cleanup_ok = 0;
        (void)cg_terminate_job(job, process, result, 125U);
        goto cleanup;
    }
    capture_output_available = options->capture_output;

    wait_result = cg_wait_for_process(
        process, completion_port, job, cpu_time_limit_enabled
            ? cpu_time_limit_ticks : 0U,
        poll_completion_port, output_failure_event,
        (DWORD)options->timeout_ms, &resource_flags, &last_error);
    if (wait_result != WAIT_FAILED && completion_port != NULL &&
        !cg_drain_resource_notifications(completion_port, &resource_flags,
                                          &last_error)) {
        wait_result = WAIT_FAILED;
    }
    if (wait_result != WAIT_FAILED &&
        (!cg_check_cpu_job_signal(job, cpu_time_limit_enabled,
                                  &resource_flags, &last_error) ||
         !cg_check_cpu_accounting(job, cpu_time_limit_enabled
                                           ? cpu_time_limit_ticks
                                           : 0U,
                                  &resource_flags, &last_error))) {
        wait_result = WAIT_FAILED;
    }
    result->resource_limit_hit = resource_flags != 0U;
    result->resource_limit_kind = cg_resource_limit_kind(resource_flags);
    if (wait_result == WAIT_TIMEOUT) {
        DWORD boundary_wait = WaitForSingleObject(process, 0);
        if (boundary_wait == WAIT_OBJECT_0) {
            wait_result = WAIT_OBJECT_0;
        } else if (boundary_wait == WAIT_FAILED) {
            last_error = GetLastError();
            wait_result = WAIT_FAILED;
        }
    }
    if (wait_result == CG_WAIT_RESOURCE ||
        ((wait_result == WAIT_TIMEOUT || wait_result == CG_WAIT_OUTPUT) &&
         resource_flags != 0U)) {
        result->status = CG_STATUS_RESOURCE_LIMIT;
        terminate_attempted = 1;
        (void)cg_terminate_job(job, process, result,
                               CG_RESOURCE_TERMINATION_CODE);
    } else if (wait_result == WAIT_TIMEOUT) {
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
    } else if (wait_result == CG_WAIT_OUTPUT) {
        LONG stdout_error = InterlockedCompareExchange(
            &stdout_capture->read_error, 0, 0);
        LONG stderr_error = InterlockedCompareExchange(
            &stderr_capture->read_error, 0, 0);

        last_error = stdout_error != (LONG)ERROR_SUCCESS
                         ? (DWORD)stdout_error
                         : (DWORD)stderr_error;
        if (last_error == ERROR_SUCCESS) {
            last_error = ERROR_READ_FAULT;
        }
        cg_set_error(result, last_error);
        result->status = CG_STATUS_INTERNAL_ERROR;
        terminate_attempted = 1;
        (void)cg_terminate_job(job, process, result, 125U);
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
        cg_collect_job_metrics(job, job_metrics,
                               hooks->query_job_information);
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
    if (stdout_capture != NULL &&
        !cg_capture_close_write(stdout_capture, &last_error) &&
        capture_output_available) {
        output_error = 1;
    }
    if (stderr_capture != NULL &&
        !cg_capture_close_write(stderr_capture, &last_error) &&
        capture_output_available) {
        output_error = 1;
    }
    if (job != NULL) {
        DWORD job_cleanup_error = ERROR_SUCCESS;
        if (job_assigned) {
            DWORD empty_error = ERROR_SUCCESS;
            if (!TerminateJobObject(job, 125U)) {
                job_cleanup_error = GetLastError();
                if (job_cleanup_error == ERROR_SUCCESS) {
                    job_cleanup_error = ERROR_GEN_FAILURE;
                }
            }
            if (!cg_wait_job_empty(job, CG_TIMEOUT_GRACE_MS, &empty_error) &&
                job_cleanup_error == ERROR_SUCCESS) {
                job_cleanup_error = empty_error;
            }
        }
        if (!CloseHandle(job) && job_cleanup_error == ERROR_SUCCESS) {
            job_cleanup_error = GetLastError();
            if (job_cleanup_error == ERROR_SUCCESS) {
                job_cleanup_error = ERROR_GEN_FAILURE;
            }
        }
        if (job_cleanup_error != ERROR_SUCCESS) {
            result->cleanup_ok = 0;
            last_error = job_cleanup_error;
            cg_set_error(result, job_cleanup_error);
        }
        job = NULL;
    }
    {
        DWORD join_error = ERROR_SUCCESS;
        if (!cg_capture_join(stdout_capture, hooks->wait_capture_thread,
                             hooks->cancel_synchronous_io,
                             hooks->capture_cancel_grace_ms, &join_error)) {
            output_error = capture_output_available;
            result->cleanup_ok = 0;
            last_error = join_error;
        } else if (join_error != ERROR_SUCCESS && capture_output_available) {
            output_error = 1;
            last_error = join_error;
        }
        join_error = ERROR_SUCCESS;
        if (!cg_capture_join(stderr_capture, hooks->wait_capture_thread,
                             hooks->cancel_synchronous_io,
                             hooks->capture_cancel_grace_ms, &join_error)) {
            output_error = capture_output_available;
            result->cleanup_ok = 0;
            last_error = join_error;
        } else if (join_error != ERROR_SUCCESS && capture_output_available) {
            output_error = 1;
            last_error = join_error;
        }
    }
    if (result->status == CG_STATUS_RESOURCE_LIMIT && !result->cleanup_ok) {
        result->status = CG_STATUS_CONTAINMENT_FAILED;
    }
    if (capture_output_available) {
        DWORD output_error_code = ERROR_SUCCESS;

        result->output_truncated =
            (stdout_capture != NULL && stdout_capture->thread_joined &&
             InterlockedCompareExchange(&stdout_capture->truncated, 0, 0) != 0) ||
            (stderr_capture != NULL && stderr_capture->thread_joined &&
             InterlockedCompareExchange(&stderr_capture->truncated, 0, 0) != 0);
        if (stdout_capture != NULL && stdout_capture->thread_joined &&
            !cg_capture_take(stdout_capture, &result->stdout_utf8,
                             &result->stdout_size, &output_error_code)) {
            output_error = 1;
            last_error = output_error_code;
        }
        if (stderr_capture != NULL && stderr_capture->thread_joined &&
            !cg_capture_take(stderr_capture, &result->stderr_utf8,
                             &result->stderr_size, &output_error_code)) {
            output_error = 1;
            last_error = output_error_code;
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
    if (thread != NULL) {
        CloseHandle(thread);
    }
    if (process != NULL) {
        CloseHandle(process);
    }
    /* An unconfirmed reader retains heap-owned state and all handles it can
       still access. This bounded quarantine is preferable to a UAF. */
    if (!cg_capture_close(stdout_capture)) {
        cg_capture_quarantine(stdout_capture);
    }
    if (!cg_capture_close(stderr_capture)) {
        cg_capture_quarantine(stderr_capture);
    }
    cg_capture_reap_quarantined();
    if (completion_port != NULL) {
        CloseHandle(completion_port);
    }
    if (output_failure_event != NULL) {
        CloseHandle(output_failure_event);
    }
    cg_builder_free(&command_line);
    return 0;
}

int cg_windows_run(const cg_run_options *options, cg_run_result *result,
                   cg_job_metrics *job_metrics)
{
    cg_windows_test_hooks hooks = cg_default_windows_hooks();
    return cg_windows_run_internal(options, result, job_metrics, &hooks);
}

#ifdef COREGUARD_TEST_HOOKS
int cg_windows_run_with_job_metrics_query_hook(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    cg_query_job_information_fn query_job_information)
{
    cg_windows_test_hooks hooks = cg_default_windows_hooks();

    if (query_job_information == NULL) {
        return -1;
    }
    hooks.query_job_information = query_job_information;
    return cg_windows_run_internal(options, result, job_metrics, &hooks);
}

int cg_windows_run_with_test_hooks(
    const cg_run_options *options,
    cg_run_result *result,
    cg_job_metrics *job_metrics,
    const cg_windows_test_hooks *test_hooks)
{
    cg_windows_test_hooks hooks = cg_default_windows_hooks();

    if (test_hooks == NULL) {
        return -1;
    }
    if (test_hooks->query_job_information != NULL) {
        hooks.query_job_information = test_hooks->query_job_information;
    }
    if (test_hooks->read_file != NULL) {
        hooks.read_file = test_hooks->read_file;
    }
    if (test_hooks->assign_process_to_job != NULL) {
        hooks.assign_process_to_job = test_hooks->assign_process_to_job;
    }
    if (test_hooks->wait_capture_thread != NULL) {
        hooks.wait_capture_thread = test_hooks->wait_capture_thread;
    }
    if (test_hooks->cancel_synchronous_io != NULL) {
        hooks.cancel_synchronous_io = test_hooks->cancel_synchronous_io;
    }
    if (test_hooks->capture_cancel_grace_ms != 0U) {
        hooks.capture_cancel_grace_ms = test_hooks->capture_cancel_grace_ms;
    }
    return cg_windows_run_internal(options, result, job_metrics, &hooks);
}

LONG cg_windows_test_quarantined_capture_count(void)
{
    return InterlockedCompareExchange(&cg_quarantined_capture_count, 0, 0);
}

LONG cg_windows_test_capture_slot_limit(void)
{
    return CG_CAPTURE_QUARANTINE_LIMIT;
}
#endif
