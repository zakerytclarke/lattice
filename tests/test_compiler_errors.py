import os
import subprocess
import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATTICE = os.path.join(PROJECT_DIR, "lattice")


def compile_file(name):
    path = os.path.join(PROJECT_DIR, "tests", name)
    return subprocess.run(
        [LATTICE, path, "10"],
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
    )


def test_unresolved_generic_error_is_actionable():
    res = compile_file("unresolved_generic_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Cannot infer" in out
    assert "hint:" in out
    assert "no heap" in out.lower() or "static" in out.lower()
    assert "foo[" in out


def test_call_arity_error_shows_signature():
    res = compile_file("call_arity_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Wrong number of arguments" in out
    assert "add_two" in out
    assert "hint:" in out


def test_const_reassign_error():
    res = compile_file("const_reassign_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "const" in out
    assert "hint:" in out


def test_list_unsafe_precondition_message():
    res = compile_file("list_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Precondition not met" in out or "Safety Error" in out
    assert "set" in out
    assert "hint:" in out


def test_string_input_requires_explicit_size():
    res = compile_file("string_input_no_size_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Cannot infer the size of this String" in out
    assert "hint:" in out
    assert "String(32)" in out


def test_list_input_requires_explicit_size():
    res = compile_file("list_input_no_size_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "list length is unknown" in out or "index bounds" in out
    assert "hint:" in out
    assert "fixed size" in out


def test_nested_list_literal_rejected():
    res = compile_file("nested_list_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Cannot infer" in out
    assert "List" in out


def test_no_double_safety_error_prefix():
    res = compile_file("list_unsafe.lattice")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Safety/Constraint Verification Error" not in out
    assert out.count("Safety Error") >= 1
