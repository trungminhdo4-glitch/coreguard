#ifdef COREGUARD_PACK_2
#pragma pack(push, 2)
#endif

#include <coreguard.h>

#ifdef COREGUARD_PACK_2
#pragma pack(pop)
#endif

#include <stddef.h>
#include <stdio.h>

#define PRINT_SIZE(type) printf("sizeof(" #type ")=%zu\n", sizeof(type))
#define PRINT_ALIGN(type) printf("alignof(" #type ")=%zu\n", _Alignof(type))
#define PRINT_OFFSET(type, field) \
    printf("offsetof(" #type "," #field ")=%zu\n", offsetof(type, field))
#define PRINT_ENUM(scope, name) \
    printf("enum(" #scope "," #name ")=%d\n", (int)(name))
#define PRINT_MACRO(name) \
    printf("macro(" #name ")=%llu\n", (unsigned long long)(name))

int main(void)
{
    printf("pointer_bits=%zu\n", sizeof(void *) * 8U);
#ifdef _MSC_VER
    printf("provenance(msc_ver)=%d\n", (int)_MSC_VER);
#endif
#ifdef _M_X64
    printf("provenance(machine)=x64\n");
#endif

    PRINT_SIZE(cg_resource_limits);
    PRINT_ALIGN(cg_resource_limits);
    PRINT_OFFSET(cg_resource_limits, memory_limit_bytes);
    PRINT_OFFSET(cg_resource_limits, valid_limits);
    PRINT_OFFSET(cg_resource_limits, reserved);
    PRINT_OFFSET(cg_resource_limits, cpu_time_limit_ms);
    PRINT_OFFSET(cg_resource_limits, active_process_limit);

    PRINT_SIZE(cg_run_options);
    PRINT_ALIGN(cg_run_options);
    PRINT_OFFSET(cg_run_options, argv);
    PRINT_OFFSET(cg_run_options, argc);
    PRINT_OFFSET(cg_run_options, timeout_ms);
    PRINT_OFFSET(cg_run_options, capture_output);
    PRINT_OFFSET(cg_run_options, resource_limits);

    PRINT_SIZE(cg_exec_context);
    PRINT_ALIGN(cg_exec_context);
    PRINT_OFFSET(cg_exec_context, working_directory);
    PRINT_OFFSET(cg_exec_context, environment_block);
    PRINT_OFFSET(cg_exec_context, environment_block_chars);
    PRINT_OFFSET(cg_exec_context, capture_prefix_bytes);

    PRINT_SIZE(cg_process_metrics);
    PRINT_ALIGN(cg_process_metrics);
    PRINT_OFFSET(cg_process_metrics, creation_time_unix_100ns);
    PRINT_OFFSET(cg_process_metrics, user_cpu_ms);
    PRINT_OFFSET(cg_process_metrics, kernel_cpu_ms);
    PRINT_OFFSET(cg_process_metrics, total_cpu_ms);
    PRINT_OFFSET(cg_process_metrics, peak_working_set_bytes);
    PRINT_OFFSET(cg_process_metrics, read_operations);
    PRINT_OFFSET(cg_process_metrics, write_operations);
    PRINT_OFFSET(cg_process_metrics, read_bytes);
    PRINT_OFFSET(cg_process_metrics, write_bytes);
    PRINT_OFFSET(cg_process_metrics, valid_fields);

    PRINT_SIZE(cg_job_metrics);
    PRINT_ALIGN(cg_job_metrics);
    PRINT_OFFSET(cg_job_metrics, snapshot_available);
    PRINT_OFFSET(cg_job_metrics, valid_fields);
    PRINT_OFFSET(cg_job_metrics, query_error);
    PRINT_OFFSET(cg_job_metrics, reserved);
    PRINT_OFFSET(cg_job_metrics, total_user_cpu_ms);
    PRINT_OFFSET(cg_job_metrics, total_kernel_cpu_ms);
    PRINT_OFFSET(cg_job_metrics, total_page_faults);
    PRINT_OFFSET(cg_job_metrics, total_processes);
    PRINT_OFFSET(cg_job_metrics, active_processes);
    PRINT_OFFSET(cg_job_metrics, total_terminated_processes);
    PRINT_OFFSET(cg_job_metrics, read_operations);
    PRINT_OFFSET(cg_job_metrics, write_operations);
    PRINT_OFFSET(cg_job_metrics, other_operations);
    PRINT_OFFSET(cg_job_metrics, read_bytes);
    PRINT_OFFSET(cg_job_metrics, write_bytes);
    PRINT_OFFSET(cg_job_metrics, other_bytes);
    PRINT_OFFSET(cg_job_metrics, peak_job_memory_used_bytes);

    PRINT_SIZE(cg_run_result);
    PRINT_ALIGN(cg_run_result);
    PRINT_OFFSET(cg_run_result, status);
    PRINT_OFFSET(cg_run_result, timed_out);
    PRINT_OFFSET(cg_run_result, resource_limit_hit);
    PRINT_OFFSET(cg_run_result, cleanup_ok);
    PRINT_OFFSET(cg_run_result, has_exit_code);
    PRINT_OFFSET(cg_run_result, exit_code);
    PRINT_OFFSET(cg_run_result, process_id);
    PRINT_OFFSET(cg_run_result, duration_ms);
    PRINT_OFFSET(cg_run_result, win32_error);
    PRINT_OFFSET(cg_run_result, stdout_utf8);
    PRINT_OFFSET(cg_run_result, stdout_size);
    PRINT_OFFSET(cg_run_result, stderr_utf8);
    PRINT_OFFSET(cg_run_result, stderr_size);
    PRINT_OFFSET(cg_run_result, output_truncated);
    PRINT_OFFSET(cg_run_result, metrics);
    PRINT_OFFSET(cg_run_result, resource_limit_kind);

    PRINT_SIZE(cg_status);
    PRINT_ENUM(cg_status, CG_STATUS_EXITED);
    PRINT_ENUM(cg_status, CG_STATUS_TIMEOUT);
    PRINT_ENUM(cg_status, CG_STATUS_START_FAILED);
    PRINT_ENUM(cg_status, CG_STATUS_CONTAINMENT_FAILED);
    PRINT_ENUM(cg_status, CG_STATUS_INTERNAL_ERROR);
    PRINT_ENUM(cg_status, CG_STATUS_USAGE_ERROR);
    PRINT_ENUM(cg_status, CG_STATUS_RESOURCE_LIMIT);

    PRINT_MACRO(CG_RESOURCE_LIMIT_MEMORY);
    PRINT_MACRO(CG_RESOURCE_LIMIT_CPU_TIME);
    PRINT_MACRO(CG_RESOURCE_LIMIT_ACTIVE_PROCESSES);
    PRINT_MACRO(CG_RESOURCE_LIMIT_KIND_NONE);
    PRINT_MACRO(CG_RESOURCE_LIMIT_KIND_MEMORY);
    PRINT_MACRO(CG_RESOURCE_LIMIT_KIND_CPU_TIME);
    PRINT_MACRO(CG_RESOURCE_LIMIT_KIND_UNKNOWN);
    PRINT_MACRO(CG_RESOURCE_LIMIT_KIND_ACTIVE_PROCESSES);
    PRINT_MACRO(CG_RESOURCE_CPU_TIME_MAX_MS);

    PRINT_MACRO(CG_CAPTURE_PREFIX_DEFAULT_BYTES);
    PRINT_MACRO(CG_CAPTURE_PREFIX_MAX_BYTES);
    PRINT_MACRO(CG_WORKDIR_MAX_CHARS);

    PRINT_MACRO(CG_PROCESS_METRIC_CREATION_TIME);
    PRINT_MACRO(CG_PROCESS_METRIC_USER_CPU_TIME);
    PRINT_MACRO(CG_PROCESS_METRIC_KERNEL_CPU_TIME);
    PRINT_MACRO(CG_PROCESS_METRIC_TOTAL_CPU_TIME);
    PRINT_MACRO(CG_PROCESS_METRIC_PEAK_WORKING_SET);
    PRINT_MACRO(CG_PROCESS_METRIC_READ_OPERATIONS);
    PRINT_MACRO(CG_PROCESS_METRIC_WRITE_OPERATIONS);
    PRINT_MACRO(CG_PROCESS_METRIC_READ_BYTES);
    PRINT_MACRO(CG_PROCESS_METRIC_WRITE_BYTES);

    PRINT_MACRO(CG_JOB_METRIC_TOTAL_USER_CPU_TIME);
    PRINT_MACRO(CG_JOB_METRIC_TOTAL_KERNEL_CPU_TIME);
    PRINT_MACRO(CG_JOB_METRIC_TOTAL_PAGE_FAULTS);
    PRINT_MACRO(CG_JOB_METRIC_TOTAL_PROCESSES);
    PRINT_MACRO(CG_JOB_METRIC_ACTIVE_PROCESSES);
    PRINT_MACRO(CG_JOB_METRIC_TOTAL_TERMINATED_PROCESSES);
    PRINT_MACRO(CG_JOB_METRIC_READ_OPERATIONS);
    PRINT_MACRO(CG_JOB_METRIC_WRITE_OPERATIONS);
    PRINT_MACRO(CG_JOB_METRIC_OTHER_OPERATIONS);
    PRINT_MACRO(CG_JOB_METRIC_READ_BYTES);
    PRINT_MACRO(CG_JOB_METRIC_WRITE_BYTES);
    PRINT_MACRO(CG_JOB_METRIC_OTHER_BYTES);
    PRINT_MACRO(CG_JOB_METRIC_PEAK_JOB_MEMORY_USED);
    PRINT_MACRO(CG_JOB_METRIC_ALL);
    return 0;
}
