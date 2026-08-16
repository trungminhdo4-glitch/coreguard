#include <coreguard.h>

int main(void)
{
    const wchar_t *argv[] = {L"cmd.exe", L"/d", L"/c", L"exit 0"};
    cg_run_options options = {argv, 4U, 5000U, 0};
    cg_run_result result = {0};
    const int rc = cg_run(&options, &result);

    if (rc != 0 || result.status != CG_STATUS_EXITED ||
        !result.has_exit_code || result.exit_code != 0U) {
        cg_run_result_free(&result);
        return 1;
    }
    cg_run_result_free(&result);
    return 0;
}
