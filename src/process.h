#ifndef COREGUARD_PROCESS_H
#define COREGUARD_PROCESS_H

#include "coreguard.h"

int cg_windows_run(const cg_run_options *options, cg_run_result *result,
                   cg_job_metrics *job_metrics);

#endif
