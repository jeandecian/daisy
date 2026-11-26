"""Pre-flight verification for proposed Python changes.

Proposed files are written to a temporary directory that mirrors their relative paths,
then checked with ``ruff check``, ``ruff format --check`` and ``mypy``. The workspace is
never touched. Only ``.py`` files are checked; other files pass through with no checks.

Each tool runs as ``python -m <tool>`` with the interpreter that runs the sidecar, so the
checks use the same pinned versions as the dev dependency group.
"""

import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

CHECK_TIMEOUT_SECONDS: Final[float] = 60.0
PYTHON_SUFFIX: Final[str] = ".py"
RUFF_LOCATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^.+:\d+:\d+: ")
RUFF_REFORMAT_PREFIX: Final[str] = "Would reformat:"
MYPY_ERROR_MARKER: Final[str] = ": error:"
MYPY_WARNING_MARKER: Final[str] = ": warning:"


class PreflightInputError(ValueError):
    """Raised when a proposed path is unsafe or the change set is empty."""


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one tool run."""

    name: str
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int | None


@dataclass(frozen=True)
class PreflightReport:
    """All checks for one change set. ``passed`` is true only when every check passed."""

    passed: bool
    checks: tuple[CheckResult, ...]

    def check(self, name: str) -> CheckResult:
        """Return the result with the given name, or raise KeyError."""
        for result in self.checks:
            if result.name == name:
                return result
        raise KeyError(name)


def validate_relpath(raw: str) -> PurePosixPath:
    """Accept only a clean relative POSIX path with no parent references."""
    if not raw or "\\" in raw:
        raise PreflightInputError(f"invalid relative path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise PreflightInputError(
            f"path must be relative and stay inside the workspace: {raw!r}"
        )
    return path


def _run_tool(
    label: str, command: list[str], cwd: Path
) -> tuple[int | None, str, str, str | None]:
    """Run a tool and return (returncode, stdout, stderr, failure_reason)."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", "", f"{label} timed out after {CHECK_TIMEOUT_SECONDS:.0f}s"
    except OSError as exc:
        return None, "", "", f"could not run {label}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr, None


def _failed(
    name: str, reason: str, stdout: str, stderr: str, returncode: int | None
) -> CheckResult:
    return CheckResult(
        name=name,
        passed=False,
        errors=(reason,),
        warnings=(),
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def _ruff_check(root: Path, files: list[str]) -> CheckResult:
    name = "ruff_check"
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--no-cache",
        "--output-format",
        "concise",
        *files,
    ]
    code, out, err, failure = _run_tool("ruff check", command, root)
    if failure is not None:
        return _failed(name, failure, out, err, code)
    if code not in (0, 1):
        return _failed(name, f"ruff check exited with code {code}", out, err, code)
    errors = tuple(
        line for line in (out + err).splitlines() if RUFF_LOCATION_PATTERN.match(line)
    )
    return CheckResult(
        name=name,
        passed=code == 0,
        errors=errors,
        warnings=(),
        stdout=out,
        stderr=err,
        returncode=code,
    )


def _ruff_format(root: Path, files: list[str]) -> CheckResult:
    name = "ruff_format"
    command = [
        sys.executable,
        "-m",
        "ruff",
        "format",
        "--check",
        "--isolated",
        "--no-cache",
        *files,
    ]
    code, out, err, failure = _run_tool("ruff format", command, root)
    if failure is not None:
        return _failed(name, failure, out, err, code)
    if code not in (0, 1):
        return _failed(name, f"ruff format exited with code {code}", out, err, code)
    errors = tuple(
        line.strip()
        for line in (out + err).splitlines()
        if line.strip().startswith(RUFF_REFORMAT_PREFIX)
    )
    return CheckResult(
        name=name,
        passed=code == 0,
        errors=errors,
        warnings=(),
        stdout=out,
        stderr=err,
        returncode=code,
    )


def _mypy(root: Path, files: list[str]) -> CheckResult:
    name = "mypy"
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--no-incremental",
        "--cache-dir",
        str(root / ".mypy_cache"),
        "--ignore-missing-imports",
        "--no-color-output",
        *files,
    ]
    code, out, err, failure = _run_tool("mypy", command, root)
    if failure is not None:
        return _failed(name, failure, out, err, code)
    if code not in (0, 1):
        return _failed(name, f"mypy exited with code {code}", out, err, code)
    lines = (out + err).splitlines()
    return CheckResult(
        name=name,
        passed=code == 0,
        errors=tuple(line for line in lines if MYPY_ERROR_MARKER in line),
        warnings=tuple(line for line in lines if MYPY_WARNING_MARKER in line),
        stdout=out,
        stderr=err,
        returncode=code,
    )


def run_preflight(changes: Mapping[str, str]) -> PreflightReport:
    """Check a set of proposed files without writing to the workspace.

    ``changes`` maps workspace-relative paths to their full proposed content. Raises
    ``PreflightInputError`` for an empty set or an unsafe path. When no ``.py`` files are
    proposed, the report passes with no checks run.
    """
    if not changes:
        raise PreflightInputError("no changes to check")
    python_files: dict[PurePosixPath, str] = {}
    for raw, content in changes.items():
        rel = validate_relpath(raw)
        if rel.suffix == PYTHON_SUFFIX:
            python_files[rel] = content
    if not python_files:
        return PreflightReport(passed=True, checks=())
    with tempfile.TemporaryDirectory(prefix="daisy-preflight-") as tmp:
        root = Path(tmp)
        for rel, content in python_files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        files = [rel.as_posix() for rel in python_files]
        checks = (
            _ruff_check(root, files),
            _ruff_format(root, files),
            _mypy(root, files),
        )
    return PreflightReport(passed=all(check.passed for check in checks), checks=checks)
