#include <coreguard.h>

int main(void)
{
    const wchar_t *argv[] = {L"cmd.exe", L"/d", L"/c", L"exit 0"};
    cg_resource_limits limits = {0};
    cg_run_options options = {0};
    cg_run_result result = {0};
    int rc;

    options.argv = argv;
    options.argc = 4U;
    options.timeout_ms = 5000U;
    options.resource_limits = &limits;
    rc = cg_run(&options, &result);
    if (rc != 0 || result.status != CG_STATUS_EXITED ||
        !result.has_exit_code || result.exit_code != 0U ||
        result.resource_limit_hit) {
        cg_run_result_free(&result);
        return 1;
    }

    cg_run_result_free(&result);
    return 0;
}
