#include <coreguard.h>

#include <string.h>

int main(void)
{
    const wchar_t *argv[] = {L"cmd.exe", L"/d", L"/c", L"exit 0"};
    cg_run_options named_options = {0};
    cg_run_options positional_options = {argv, 4U, 5000U, 0};
    cg_run_result result;
    int rc;

    named_options.argv = argv;
    named_options.argc = 4U;
    named_options.timeout_ms = 5000U;
    named_options.capture_output = 0;
    memset(&result, 0, sizeof(result));

    rc = cg_run(&positional_options, &result);
    if (rc != 0 || result.status != CG_STATUS_EXITED ||
        !result.has_exit_code || result.exit_code != 0U) {
        cg_run_result_free(&result);
        return 1;
    }

    cg_run_result_free(&result);
    return named_options.resource_limits == NULL ? 0 : 1;
}
