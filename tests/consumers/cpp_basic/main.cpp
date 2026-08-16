#include <coreguard.h>

int main()
{
    const wchar_t *argv[] = {L"cmd.exe", L"/d", L"/c", L"exit 0"};
    cg_run_options options{};
    cg_run_result result{};

    options.argv = argv;
    options.argc = 4U;
    options.timeout_ms = 5000U;
    const int rc = cg_run(&options, &result);
    const bool ok = rc == 0 && result.status == CG_STATUS_EXITED &&
                    result.has_exit_code != 0 && result.exit_code == 0U;
    cg_run_result_free(&result);
    return ok ? 0 : 1;
}
