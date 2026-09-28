/*
 * Compile-only public signature check for the frozen v0.2.0 contract.
 *
 * The translation unit is compiled with /c and never linked or run. Every
 * public function is checked against its exact function-pointer type with a
 * C11 _Generic static assertion, so return type, parameter types, constness,
 * and calling convention drift all become compile errors. MSVC accepts an
 * incompatible return type in a plain function-pointer assignment without a
 * diagnostic, which is why the assignment form alone is not sufficient.
 */

#include <coreguard.h>

#define CG_ASSERT_SIGNATURE(function, type) \
    _Static_assert(                         \
        _Generic(&(function), type: 1, default: 0), \
        "public signature drift: " #function)

CG_ASSERT_SIGNATURE(cg_run, int (*)(const cg_run_options *, cg_run_result *));
CG_ASSERT_SIGNATURE(cg_run_with_job_metrics,
                    int (*)(const cg_run_options *, cg_run_result *, cg_job_metrics *));
CG_ASSERT_SIGNATURE(
    cg_run_ex, int (*)(const cg_run_options *, const cg_exec_context *, cg_run_result *));
CG_ASSERT_SIGNATURE(cg_run_ex_with_job_metrics,
                    int (*)(const cg_run_options *,
                            const cg_exec_context *,
                            cg_run_result *,
                            cg_job_metrics *));
CG_ASSERT_SIGNATURE(cg_run_result_free, void (*)(cg_run_result *));
CG_ASSERT_SIGNATURE(cg_status_name, const char *(*)(cg_status));

int main(void)
{
    return 0;
}
