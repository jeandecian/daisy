"""Tests for the pre-flight checks engine (ruff and mypy) and Python auto-formatting."""

import importlib.util

import pytest

from daisy.preflight import (
    PreflightInputError,
    autoformat_python,
    run_preflight,
    validate_relpath,
)

requires_tools = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None or importlib.util.find_spec("mypy") is None,
    reason="ruff and mypy are dev dependencies",
)

CLEAN = "def add(a: int, b: int) -> int:\n    return a + b\n"


@pytest.mark.parametrize("raw", ["", "/etc/x.py", "../x.py", "a/../../x.py", "a\\b.py"])
def test_unsafe_paths_are_rejected(raw: str) -> None:
    with pytest.raises(PreflightInputError):
        validate_relpath(raw)


def test_clean_relative_path_is_accepted() -> None:
    assert validate_relpath("pkg/mod.py").as_posix() == "pkg/mod.py"


def test_empty_changes_are_rejected() -> None:
    with pytest.raises(PreflightInputError):
        run_preflight({})


def test_unsafe_path_in_changes_is_rejected() -> None:
    with pytest.raises(PreflightInputError):
        run_preflight({"../evil.py": CLEAN})


@requires_tools
def test_non_python_only_passes_without_running_checks() -> None:
    report = run_preflight({"notes.txt": "hello\n"})

    assert report.passed is True
    assert report.checks == ()


@requires_tools
def test_clean_code_passes_all_checks() -> None:
    report = run_preflight({"pkg/clean.py": CLEAN})

    assert report.passed is True
    assert [check.name for check in report.checks] == ["ruff_check", "ruff_format", "mypy"]
    assert all(check.passed for check in report.checks)
    assert all(check.errors == () for check in report.checks)
    assert all(check.returncode == 0 for check in report.checks)


@requires_tools
def test_lint_error_is_reported_structurally() -> None:
    report = run_preflight({"bad.py": "import os\n\nVALUE = 1\n"})

    lint = report.check("ruff_check")
    assert lint.passed is False
    assert lint.returncode == 1
    assert any("F401" in error for error in lint.errors)
    assert report.passed is False


@requires_tools
def test_lint_result_carries_tool_output() -> None:
    report = run_preflight({"bad.py": "import os\n\nVALUE = 1\n"})

    lint = report.check("ruff_check")
    assert lint.stdout or lint.stderr


@requires_tools
def test_format_error_is_reported_without_failing_lint() -> None:
    report = run_preflight({"loose.py": "x=1\n"})

    fmt = report.check("ruff_format")
    assert fmt.passed is False
    assert any("loose.py" in error for error in fmt.errors)
    assert report.check("ruff_check").passed is True


@requires_tools
def test_type_error_is_reported() -> None:
    report = run_preflight({"typed.py": "def f() -> int:\n    return 'a'\n"})

    types = report.check("mypy")
    assert types.passed is False
    assert any(": error:" in error for error in types.errors)
    assert report.passed is False


@requires_tools
def test_autoformat_rewrites_loose_python() -> None:
    formatted = autoformat_python({"loose.py": "x=1\n"})

    assert formatted == {"loose.py": "x = 1\n"}


@requires_tools
def test_autoformat_makes_format_check_pass() -> None:
    formatted = autoformat_python({"loose.py": "x=1\n"})
    report = run_preflight(formatted)

    assert report.check("ruff_format").passed is True
    assert report.check("ruff_check").passed is True


def test_autoformat_leaves_non_python_untouched() -> None:
    changes = {"notes.txt": "x=1\n"}

    assert autoformat_python(changes) == changes


@requires_tools
def test_autoformat_keeps_content_when_python_has_syntax_error() -> None:
    broken = "def (:\n"

    assert autoformat_python({"broken.py": broken}) == {"broken.py": broken}


@requires_tools
def test_autoformat_rejects_unsafe_path() -> None:
    with pytest.raises(PreflightInputError):
        autoformat_python({"../evil.py": "x=1\n"})
