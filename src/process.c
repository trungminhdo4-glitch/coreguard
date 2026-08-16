#include "process.h"

#include <stdlib.h>
#include <string.h>

const char *cg_status_name(cg_status status)
{
    switch (status) {
    case CG_STATUS_EXITED:
        return "exited";
    case CG_STATUS_TIMEOUT:
        return "timeout";
    case CG_STATUS_START_FAILED:
        return "start_failed";
    case CG_STATUS_CONTAINMENT_FAILED:
        return "containment_failed";
    case CG_STATUS_INTERNAL_ERROR:
        return "internal_error";
    case CG_STATUS_USAGE_ERROR:
        return "usage_error";
    case CG_STATUS_RESOURCE_LIMIT:
        return "resource_limit";
    default:
        return "unknown";
    }
}

static int cg_resource_limits_valid(const cg_resource_limits *limits)
{
    if (limits == NULL) {
        return 1;
    }
    if (limits->reserved != 0U ||
        (limits->valid_limits &
         ~(CG_RESOURCE_LIMIT_MEMORY | CG_RESOURCE_LIMIT_CPU_TIME |
           CG_RESOURCE_LIMIT_ACTIVE_PROCESSES)) != 0U) {
        return 0;
    }
    if ((limits->valid_limits & CG_RESOURCE_LIMIT_MEMORY) != 0U) {
        if (limits->memory_limit_bytes == 0U ||
            limits->memory_limit_bytes == UINT64_MAX) {
            return 0;
        }
    } else if (limits->memory_limit_bytes != 0U) {
        return 0;
    }
    if ((limits->valid_limits & CG_RESOURCE_LIMIT_CPU_TIME) != 0U) {
        if (limits->cpu_time_limit_ms == 0U ||
            limits->cpu_time_limit_ms > CG_RESOURCE_CPU_TIME_MAX_MS) {
            return 0;
        }
    } else if (limits->cpu_time_limit_ms != 0U) {
        return 0;
    }
    if ((limits->valid_limits & CG_RESOURCE_LIMIT_ACTIVE_PROCESSES) != 0U) {
        if (limits->active_process_limit == 0U) {
            return 0;
        }
    } else if (limits->active_process_limit != 0U) {
        return 0;
    }
    return 1;
}

void cg_run_result_free(cg_run_result *result)
{
    if (result == NULL) {
        return;
    }
    free(result->stdout_utf8);
    free(result->stderr_utf8);
    result->stdout_utf8 = NULL;
    result->stderr_utf8 = NULL;
    result->stdout_size = 0;
    result->stderr_size = 0;
}

static int cg_run_internal(const cg_run_options *options,
                           cg_run_result *result,
                           cg_job_metrics *job_metrics)
{
    if (result == NULL) {
        return -1;
    }
    memset(result, 0, sizeof(*result));
    result->status = CG_STATUS_INTERNAL_ERROR;
    result->cleanup_ok = 1;
    if (job_metrics != NULL) {
        memset(job_metrics, 0, sizeof(*job_metrics));
    }

    if (options == NULL || options->argv == NULL || options->argc == 0 ||
        options->timeout_ms == 0 || options->timeout_ms > UINT32_MAX ||
        !cg_resource_limits_valid(options->resource_limits)) {
        result->status = CG_STATUS_USAGE_ERROR;
        return 0;
    }

#ifdef _WIN32
    return cg_windows_run(options, result, job_metrics);
#else
    (void)options;
    (void)job_metrics;
    result->status = CG_STATUS_INTERNAL_ERROR;
    return 0;
#endif
}

int cg_run(const cg_run_options *options, cg_run_result *result)
{
    return cg_run_internal(options, result, NULL);
}

int cg_run_with_job_metrics(const cg_run_options *options,
                            cg_run_result *result,
                            cg_job_metrics *job_metrics)
{
    if (job_metrics == NULL) {
        return -1;
    }
    return cg_run_internal(options, result, job_metrics);
}
