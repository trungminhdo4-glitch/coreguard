#ifndef COREGUARD_H
#define COREGUARD_H

#include <stddef.h>
#include <stdint.h>
#include <wchar.h>

typedef enum cg_status {
    CG_STATUS_EXITED = 0,
    CG_STATUS_TIMEOUT,
    CG_STATUS_START_FAILED,
    CG_STATUS_CONTAINMENT_FAILED,
    CG_STATUS_INTERNAL_ERROR,
    CG_STATUS_USAGE_ERROR,
    CG_STATUS_RESOURCE_LIMIT
} cg_status;

#define CG_RESOURCE_LIMIT_MEMORY UINT32_C(1)
#define CG_RESOURCE_LIMIT_CPU_TIME UINT32_C(2)
#define CG_RESOURCE_LIMIT_ACTIVE_PROCESSES UINT32_C(4)
#define CG_RESOURCE_LIMIT_KIND_NONE UINT32_C(0)
#define CG_RESOURCE_LIMIT_KIND_MEMORY UINT32_C(1)
#define CG_RESOURCE_LIMIT_KIND_CPU_TIME UINT32_C(2)
#define CG_RESOURCE_LIMIT_KIND_UNKNOWN UINT32_C(3)
#define CG_RESOURCE_LIMIT_KIND_ACTIVE_PROCESSES UINT32_C(4)

/* Maximum public CPU-time value that fits in a positive Windows LARGE_INTEGER
   after conversion to 100-nanosecond units. */
#define CG_RESOURCE_CPU_TIME_MAX_MS \
    (UINT64_C(0x7fffffffffffffff) / UINT64_C(10000))

typedef struct cg_resource_limits {
    /* Job-wide committed-memory limit in bytes; only active when valid_limits
       contains CG_RESOURCE_LIMIT_MEMORY. */
    uint64_t memory_limit_bytes;
    uint32_t valid_limits;
    uint32_t reserved;
    /* Job-wide user-mode CPU-time limit in milliseconds; only active when
       valid_limits contains CG_RESOURCE_LIMIT_CPU_TIME. */
    uint64_t cpu_time_limit_ms;
    /* Maximum simultaneously active processes in the job; only active when
       valid_limits contains CG_RESOURCE_LIMIT_ACTIVE_PROCESSES. The root
       process counts as one active process. */
    uint32_t active_process_limit;
} cg_resource_limits;

typedef struct cg_run_options {
    const wchar_t *const *argv;
    size_t argc;
    uint64_t timeout_ms;
    int capture_output;
    const cg_resource_limits *resource_limits;
} cg_run_options;

#define CG_PROCESS_METRIC_CREATION_TIME UINT32_C(1)
#define CG_PROCESS_METRIC_USER_CPU_TIME UINT32_C(2)
#define CG_PROCESS_METRIC_KERNEL_CPU_TIME UINT32_C(4)
#define CG_PROCESS_METRIC_TOTAL_CPU_TIME UINT32_C(8)
#define CG_PROCESS_METRIC_PEAK_WORKING_SET UINT32_C(16)
#define CG_PROCESS_METRIC_READ_OPERATIONS UINT32_C(32)
#define CG_PROCESS_METRIC_WRITE_OPERATIONS UINT32_C(64)
#define CG_PROCESS_METRIC_READ_BYTES UINT32_C(128)
#define CG_PROCESS_METRIC_WRITE_BYTES UINT32_C(256)

typedef struct cg_process_metrics {
    /* Unix epoch in 100-nanosecond ticks; preserves FILETIME precision. */
    uint64_t creation_time_unix_100ns;
    /* CPU time, not wall time or utilization. */
    uint64_t user_cpu_ms;
    uint64_t kernel_cpu_ms;
    uint64_t total_cpu_ms;
    /* Root payload process only; no child/tree aggregation. */
    uint64_t peak_working_set_bytes;
    uint64_t read_operations;
    uint64_t write_operations;
    uint64_t read_bytes;
    uint64_t write_bytes;
    uint32_t valid_fields;
} cg_process_metrics;

#define CG_JOB_METRIC_TOTAL_USER_CPU_TIME UINT32_C(1)
#define CG_JOB_METRIC_TOTAL_KERNEL_CPU_TIME UINT32_C(2)
#define CG_JOB_METRIC_TOTAL_PAGE_FAULTS UINT32_C(4)
#define CG_JOB_METRIC_TOTAL_PROCESSES UINT32_C(8)
#define CG_JOB_METRIC_ACTIVE_PROCESSES UINT32_C(16)
#define CG_JOB_METRIC_TOTAL_TERMINATED_PROCESSES UINT32_C(32)
#define CG_JOB_METRIC_READ_OPERATIONS UINT32_C(64)
#define CG_JOB_METRIC_WRITE_OPERATIONS UINT32_C(128)
#define CG_JOB_METRIC_OTHER_OPERATIONS UINT32_C(256)
#define CG_JOB_METRIC_READ_BYTES UINT32_C(512)
#define CG_JOB_METRIC_WRITE_BYTES UINT32_C(1024)
#define CG_JOB_METRIC_OTHER_BYTES UINT32_C(2048)
#define CG_JOB_METRIC_PEAK_JOB_MEMORY_USED UINT32_C(4096)

#define CG_JOB_METRIC_ALL                                                   \
    (CG_JOB_METRIC_TOTAL_USER_CPU_TIME | CG_JOB_METRIC_TOTAL_KERNEL_CPU_TIME | \
     CG_JOB_METRIC_TOTAL_PAGE_FAULTS | CG_JOB_METRIC_TOTAL_PROCESSES |       \
     CG_JOB_METRIC_ACTIVE_PROCESSES |                                        \
     CG_JOB_METRIC_TOTAL_TERMINATED_PROCESSES |                               \
     CG_JOB_METRIC_READ_OPERATIONS | CG_JOB_METRIC_WRITE_OPERATIONS |        \
     CG_JOB_METRIC_OTHER_OPERATIONS | CG_JOB_METRIC_READ_BYTES |             \
     CG_JOB_METRIC_WRITE_BYTES | CG_JOB_METRIC_OTHER_BYTES |                 \
     CG_JOB_METRIC_PEAK_JOB_MEMORY_USED)

typedef struct cg_job_metrics {
    /* One native snapshot taken while the Job Object handle is still open. */
    uint32_t snapshot_available;
    /* A field is usable only when its bit is present in valid_fields. */
    uint32_t valid_fields;
    /* First Win32 error from a failed metrics query, or zero if none. */
    uint32_t query_error;
    uint32_t reserved;
    /* Lifetime aggregate, converted from native 100-nanosecond ticks to ms. */
    uint64_t total_user_cpu_ms;
    uint64_t total_kernel_cpu_ms;
    /* Native DWORD counters; TotalTerminatedProcesses is limit-caused only. */
    uint32_t total_page_faults;
    uint32_t total_processes;
    uint32_t active_processes;
    uint32_t total_terminated_processes;
    /* Lifetime aggregate I/O counters for all processes in the job. */
    uint64_t read_operations;
    uint64_t write_operations;
    uint64_t other_operations;
    uint64_t read_bytes;
    uint64_t write_bytes;
    uint64_t other_bytes;
    /* PeakJobMemoryUsed, in bytes, as reported at the snapshot. */
    uint64_t peak_job_memory_used_bytes;
} cg_job_metrics;

typedef struct cg_run_result {
    cg_status status;
    int timed_out;
    /* Native resource-limit evidence was observed. */
    int resource_limit_hit;
    int cleanup_ok;
    int has_exit_code;
    uint32_t exit_code;
    uint32_t process_id;
    uint64_t duration_ms;
    uint32_t win32_error;
    char *stdout_utf8;
    size_t stdout_size;
    char *stderr_utf8;
    size_t stderr_size;
    int output_truncated;
    cg_process_metrics metrics;
    /* CG_RESOURCE_LIMIT_KIND_*; multiple native causes are unknown. */
    uint32_t resource_limit_kind;
} cg_run_result;

#if defined(_MSC_VER) && defined(_M_X64)
#if defined(__cplusplus)
#define CG_ABI_STATIC_ASSERT static_assert
#define CG_ABI_ALIGNOF(type) alignof(type)
#else
#define CG_ABI_STATIC_ASSERT _Static_assert
#define CG_ABI_ALIGNOF(type) _Alignof(type)
#endif

/*
 * The supported public ABI is the default MSVC x64 layout. Rejecting a
 * changed packing mode at compile time prevents a pointer/struct mismatch
 * from becoming a silent consumer failure.
 */
CG_ABI_STATIC_ASSERT(sizeof(cg_status) == 4, "Coreguard expects a 32-bit MSVC enum");
CG_ABI_STATIC_ASSERT(sizeof(cg_resource_limits) == 32,
                     "Coreguard requires default x64 struct packing");
CG_ABI_STATIC_ASSERT(CG_ABI_ALIGNOF(cg_resource_limits) == 8,
                     "Coreguard requires default x64 struct alignment");
CG_ABI_STATIC_ASSERT(sizeof(cg_run_options) == 40,
                     "Coreguard requires default x64 options layout");
CG_ABI_STATIC_ASSERT(offsetof(cg_run_options, resource_limits) == 32,
                     "Coreguard requires the x64 resource_limits offset");
CG_ABI_STATIC_ASSERT(sizeof(cg_process_metrics) == 80,
                     "Coreguard requires default x64 process metrics layout");
CG_ABI_STATIC_ASSERT(sizeof(cg_job_metrics) == 104,
                     "Coreguard requires default x64 job metrics layout");
CG_ABI_STATIC_ASSERT(CG_ABI_ALIGNOF(cg_job_metrics) == 8,
                     "Coreguard requires default x64 job metrics alignment");
CG_ABI_STATIC_ASSERT(sizeof(cg_run_result) == 176,
                     "Coreguard requires default x64 result layout");
CG_ABI_STATIC_ASSERT(offsetof(cg_run_result, duration_ms) == 32,
                     "Coreguard requires the x64 duration_ms offset");

#undef CG_ABI_STATIC_ASSERT
#undef CG_ABI_ALIGNOF
#endif

#ifdef __cplusplus
extern "C" {
#endif

int cg_run(const cg_run_options *options, cg_run_result *result);
/* Opt-in variant that returns one native aggregate Job Object snapshot. */
int cg_run_with_job_metrics(const cg_run_options *options,
                            cg_run_result *result,
                            cg_job_metrics *job_metrics);
void cg_run_result_free(cg_run_result *result);
const char *cg_status_name(cg_status status);

#ifdef __cplusplus
}
#endif

#endif
