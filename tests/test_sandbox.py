"""Sandbox behaviour across supported platforms with real or mocked runtimes."""
import os
import sys
import time

import pytest

from app.analyzer.driver_synthesizer import synthesize_driver
from app.analyzer.gcc_sandbox import (
    diagnose_case_failure,
    gcc_available,
    outputs_match,
    run_gcc_verification,
)
from app.schemas import TestCase as Case
from app.security import harden_process

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux OS features")
linux_gcc_required = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and gcc_available()), reason="needs Linux and gcc"
)


def case(inp="", out="x", sample=False):
    return Case(input_data=inp, expected_output=out, is_sample=sample)


ECHO = '#include <stdio.h>\nint main() {\n    int n;\n    scanf("%d", &n);\n    printf("%d\\n", n * 2);\n    return 0;\n}\n'


@linux_gcc_required
def test_correct_program_passes_and_counts_sample_and_hidden():
    result = run_gcc_verification(ECHO, [case("5\n", "10", True), case("21\n", "42")])
    assert result.compiled and (result.passed_count, result.total_count) == (2, 2)
    assert (result.sample_passed, result.sample_total, result.hidden_passed, result.hidden_total) == (1, 1, 1, 1)


@linux_gcc_required
def test_wrong_output_fails_and_is_described():
    result = run_gcc_verification(ECHO, [case("5\n", "11")])
    assert result.passed_count == 0 and result.case_details[0]["actual"] == "10" and result.case_details[0]["expected"] == "11"


@linux_gcc_required
def test_compile_error_is_reported_without_running_anything():
    result = run_gcc_verification("int main() { int x = ; }\n", [case()])
    assert result.compiled is False and "error" in result.compiler_errors.lower()
    assert "submission.c" in result.compiler_errors and "/tmp" not in result.compiler_errors


def test_no_test_cases_means_nothing_was_verified():
    result = run_gcc_verification(ECHO, [])
    assert result.total_count == 0 and result.skipped_reason


@linux_gcc_required
def test_infinite_loop_times_out_quickly():
    started = time.time()
    result = run_gcc_verification("int main() { for (;;) {} }\n", [case()], timeout_sec=1.0)
    assert result.timed_out is True and result.passed_count == 0
    assert time.time() - started < 8


@linux_gcc_required
def test_program_that_prints_then_sleeps_is_cut_off_at_the_timeout():
    code = '#include <stdio.h>\n#include <unistd.h>\nint main() {\n    printf("hello\\n");\n    fflush(stdout);\n    sleep(20);\n    return 0;\n}\n'
    started = time.time()
    result = run_gcc_verification(code, [case()], timeout_sec=1.0)
    assert result.timed_out is True and time.time() - started < 8     # the old buffered read() would have blocked here


@linux_gcc_required
def test_output_flood_is_truncated():
    code = '#include <stdio.h>\nint main() { for (;;) puts("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"); }\n'
    result = run_gcc_verification(code, [case()])
    assert "TRUNCATED" in result.case_details[0]["actual"] and result.passed_count == 0


@linux_gcc_required
def test_non_utf8_output_does_not_crash_the_evaluation():
    result = run_gcc_verification('#include <stdio.h>\nint main() { putchar(0xff); putchar(10); return 0; }\n', [case()])
    assert result.compiled and result.passed_count == 0


@linux_gcc_required
def test_nonzero_exit_code_fails_the_test():
    result = run_gcc_verification('int main() { return 3; }\n', [case("", "")])
    assert result.passed_count == 0 and result.runtime_error is True


@linux_only
def test_server_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-student-code")
    code = '#include <stdio.h>\n#include <stdlib.h>\nint main() {\n    const char *k = getenv("ANTHROPIC_API_KEY");\n    printf("%s\\n", k ? k : "unset");\n    return 0;\n}\n'
    result = run_gcc_verification(code, [case("", "unset")])
    assert result.passed_count == 1


@linux_only
def test_server_process_environment_is_unreadable_after_hardening():
    if os.geteuid() == 0:
        pytest.skip("root can read /proc/<pid>/environ regardless")
    if not harden_process():
        pytest.skip("prctl(PR_SET_DUMPABLE) is not permitted in this environment")
    code = r'''#include <stdio.h>
#include <unistd.h>
int main() {
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/environ", (int)getppid());
    FILE *f = fopen(path, "r");
    puts(f ? "readable" : "denied");
    return 0;
}
'''
    result = run_gcc_verification(code, [case("", "denied")])
    assert result.passed_count == 1, result.case_details


@linux_only
def test_runaway_compilation_is_killed_and_leaves_no_compiler_behind():
    levels = 24
    macros = "".join(f"#define M{i} M{i + 1} M{i + 1}\n" for i in range(levels)) + f"#define M{levels} 1\n"
    code = macros + "int v[] = { M0 };\nint main() { return 0; }\n"
    from app.config import settings
    old = settings.compile_timeout_seconds
    settings.compile_timeout_seconds = 3.0
    try:
        started = time.time()
        result = run_gcc_verification(code, [case()])
    finally:
        settings.compile_timeout_seconds = old
    assert result.compiled is False and time.time() - started < 15
    time.sleep(0.5)
    survivors = os.popen("pgrep -f 'cc[1] .*iiserb_c_sandbox' || true").read().strip()
    assert survivors == "", survivors


def test_python_sandbox_execution_and_failure_diagnosis():
    py_code = "n = int(input())\nprint(n * 2)\n"
    res = run_gcc_verification(py_code, [case("5\n", "10", True), case("21\n", "43")], language="python")
    assert res.compiled and "python" in res.runtime_used
    assert (res.passed_count, res.total_count) == (1, 2)
    assert "OFF_BY_ONE" in res.failure_modes


def test_unknown_language_gracefully_skips_sandbox():
    res = run_gcc_verification("SELECT 1;", [case("1\n", "1")], language="sql")
    assert res.compiled is True and res.total_count == 0 and "No local sql" in res.skipped_reason


def test_large_output_exceeding_2048_bytes_is_not_truncated_during_match():
    # 500 lines of numbers (approx 3500 bytes)
    expected_lines = "\n".join(f"Item {i}: result={i * 2}" for i in range(500))
    py_code = "for i in range(500):\n    print(f'Item {i}: result={i * 2}')\n"
    res = run_gcc_verification(py_code, [case("", expected_lines)], language="python")
    assert res.compiled and res.passed_count == 1
    assert res.case_details[0]["passed"] is True


def test_outputs_match_scientific_notation_and_tolerances():
    assert outputs_match("1e-5", "0.000010001", rel_tol=1e-4) is True
    assert outputs_match("1e4", "10000.0") is True
    assert outputs_match("inf", "inf") is True
    assert outputs_match("true", "True") is True
    assert outputs_match("3.141590", "3.14159") is True
    assert outputs_match("1 2 3", "1\n2\n3") is True
    assert outputs_match("1e-5", "1e-4", rel_tol=1e-6) is False


def test_multi_token_failure_diagnosis():
    assert diagnose_case_failure("1 2 3", "1 2 4", 0, False, False) == "OFF_BY_ONE"
    assert diagnose_case_failure("100 200 300", "100 199 300", 0, False, False) == "OFF_BY_ONE"
    assert diagnose_case_failure("Result is 42", "42", 0, False, False) == "CASE_OR_FORMAT_MISMATCH"
    assert diagnose_case_failure("Total: 5", "5", 0, False, False) == "CASE_OR_FORMAT_MISMATCH"
    assert diagnose_case_failure("PASS", "pass", 0, False, False) == "CASE_OR_FORMAT_MISMATCH"


def test_function_driver_synthesis_python():
    fn_code = "def solve(a, b):\n    return a * b + 1\n"
    synth, was_synth = synthesize_driver(fn_code, "python")
    assert was_synth and "__main__" in synth
    res = run_gcc_verification(synth, [case("3 4\n", "13", True), case("5 6\n", "31")], language="python")
    assert res.compiled and res.passed_count == 2


def test_function_driver_synthesis_cpp():
    fn_code = "int solve(int a, int b) {\n    return a + b;\n}\n"
    synth, was_synth = synthesize_driver(fn_code, "cpp")
    assert was_synth and "main" in synth
    assert "solve(a, b)" in synth or "solve(" in synth
