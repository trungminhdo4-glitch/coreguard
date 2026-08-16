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

int main(void)
{
    printf("pointer_bits=%zu\n", sizeof(void *) * 8U);

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
    return 0;
}
