#include <coreguard.h>

#include <stddef.h>
#include <stdio.h>

int main(void)
{
    printf("sizeof(cg_run_options)=%zu\n", sizeof(cg_run_options));
    printf("offsetof(cg_run_options,capture_output)=%zu\n",
           offsetof(cg_run_options, capture_output));
    printf("sizeof(cg_run_result)=%zu\n", sizeof(cg_run_result));
    printf("offsetof(cg_run_result,cleanup_ok)=%zu\n",
           offsetof(cg_run_result, cleanup_ok));
    printf("offsetof(cg_run_result,metrics)=%zu\n",
           offsetof(cg_run_result, metrics));
    return 0;
}
