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

#define CG_RESOURCE_CPU_TIME_MAX_MS \
    (UINT64_C(0x7fffffffffffffff) / UINT64_C(10000))

typedef struct cg_resource_limits {
    uint64_t memory_limit_bytes;
    uint32_t valid_limits;
    uint32_t reserved;
    uint64_t cpu_time_limit_ms;
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
    uint64_t creation_time_unix_100ns;
    uint64_t user_cpu_ms;
    uint64_t kernel_cpu_ms;
    uint64_t total_cpu_ms;
    uint64_t peak_working_set_bytes;
    uint64_t read_operations;
    uint64_t write_operations;
    uint64_t read_bytes;
    uint64_t write_bytes;
    uint32_t valid_fields;
} cg_process_metrics;

typedef struct cg_run_result {
    cg_status status;
    int timed_out;
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
    uint32_t resource_limit_kind;
} cg_run_result;

int cg_run(const cg_run_options *options, cg_run_result *result);
void cg_run_result_free(cg_run_result *result);
const char *cg_status_name(cg_status status);

#endif
