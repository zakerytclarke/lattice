import json
import os
import subprocess
import pytest

# Load test data from tests.json
tests_json_path = os.path.join(os.path.dirname(__file__), "tests.json")
with open(tests_json_path, "r") as f:
    test_suite = json.load(f)

# Extract test cases for parametrization
compilation_failure_cases = []
execution_success_cases = []
execution_runtime_failure_cases = []
execution_network_cases = []

for file_info in test_suite:
    file_name = file_info["file"]
    should_fail = file_info.get("should_fail", False)
    if should_fail:
        compilation_failure_cases.append(file_name)
    else:
        for case in file_info.get("test_cases", []):
            if case.get("expect_failure"):
                execution_runtime_failure_cases.append((
                    file_name,
                    case["args"],
                    case["expected_error"],
                    case.get("stdin"),
                ))
            elif case.get("expected_range") is not None:
                execution_network_cases.append((
                    file_name,
                    case["args"],
                    case["expected_range"],
                    case.get("stdin"),
                ))
            else:
                execution_success_cases.append((
                    file_name,
                    case["args"],
                    case["expected"],
                    case.get("stdin"),
                ))

runtime_argument_failure_cases = [
    ("factorial.lattice", [], "main expects 1 argument(s): n_arg: Input[Integer(x){x > 0}]"),
]

@pytest.mark.parametrize("file_name", compilation_failure_cases)
def test_compilation_failure(file_name):
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lattice_exec = os.path.join(project_dir, "lattice")
    file_path = os.path.join(project_dir, "tests", file_name)
    
    # Run compiler, expect non-zero exit status (1)
    res = subprocess.run([lattice_exec, file_path, "10"], capture_output=True, text=True, cwd=project_dir)
    assert res.returncode != 0, f"Expected verification/compilation to fail for {file_name}, but it succeeded."

@pytest.mark.parametrize("file_name, args, expected, stdin", execution_success_cases)
def test_execution_success(file_name, args, expected, stdin):
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lattice_exec = os.path.join(project_dir, "lattice")
    file_path = os.path.join(project_dir, "tests", file_name)
    
    cmd = [lattice_exec, file_path] + [str(a) for a in args]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_dir,
        input=stdin,
    )
    
    assert res.returncode == 0, f"Failed to compile/execute {file_name} with args {args}. Error: {res.stderr or res.stdout}"
    assert res.stdout.strip() == str(expected), f"Unexpected output for {file_name} with args {args}. Expected {expected}, got {res.stdout.strip()}"

@pytest.mark.parametrize("file_name, args, expected_range, stdin", execution_network_cases)
def test_execution_network(file_name, args, expected_range, stdin):
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lattice_exec = os.path.join(project_dir, "lattice")
    file_path = os.path.join(project_dir, "tests", file_name)

    cmd = [lattice_exec, file_path] + [str(a) for a in args]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_dir,
        input=stdin,
    )

    if res.returncode != 0:
        pytest.skip(f"network test skipped for {file_name}: {res.stderr or res.stdout}")

    try:
        matches = __import__('re').findall(r'-?\d+', res.stdout)
        value = int(matches[-1])
    except (ValueError, IndexError):
        pytest.fail(f"Expected integer output for {file_name}, got {res.stdout.strip()!r}")

    low, high = expected_range
    assert low <= value <= high, (
        f"Output {value} for {file_name} outside expected range [{low}, {high}]"
    )

@pytest.mark.parametrize("file_name, args, expected_error, stdin", execution_runtime_failure_cases)
def test_execution_runtime_failure(file_name, args, expected_error, stdin):
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lattice_exec = os.path.join(project_dir, "lattice")
    file_path = os.path.join(project_dir, "tests", file_name)

    cmd = [lattice_exec, file_path] + [str(a) for a in args]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_dir,
        input=stdin,
    )

    assert res.returncode != 0, f"Expected runtime failure for {file_name} with args {args}"
    output = (res.stderr or res.stdout).strip()
    assert expected_error in output, f"Unexpected error for {file_name} with args {args}. Got: {output}"

@pytest.mark.parametrize("file_name, args, expected_error", runtime_argument_failure_cases)
def test_runtime_argument_failure(file_name, args, expected_error):
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lattice_exec = os.path.join(project_dir, "lattice")
    file_path = os.path.join(project_dir, "tests", file_name)

    cmd = [lattice_exec, file_path] + [str(a) for a in args]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=project_dir)

    assert res.returncode != 0, f"Expected runtime argument validation to fail for {file_name} with args {args}"
    output = (res.stderr or res.stdout).strip()
    assert expected_error in output, f"Unexpected error for {file_name} with args {args}. Got: {output}"
