"""Tests for the extensions/ dynamic loader."""

import os
import sys
from pathlib import Path

import pytest
from daisy.extensions import (
    EXTENSIONS_DIRNAME,
    MODULE_PREFIX,
    ExtensionError,
    discover_extensions,
    load_extension,
    validate_meta,
)

GOOD_TEMPLATE = (
    'EXTENSION_META = {"name": "NAME", "version": "1.0.0", "description": "Test NAME."}\n'
    "\n"
    "def run(payload):\n"
    '    return {"echo": payload}\n'
)


def _good(name: str) -> str:
    return GOOD_TEMPLATE.replace("NAME", name)


def _write(workspace: Path, filename: str, body: str) -> Path:
    folder = workspace / EXTENSIONS_DIRNAME
    folder.mkdir(exist_ok=True)
    path = folder / filename
    path.write_text(body, encoding="utf-8")
    return path


def _leaked_modules() -> set[str]:
    return {key for key in sys.modules if key.startswith(MODULE_PREFIX)}


def test_discovers_and_runs_valid_extension(tmp_path: Path) -> None:
    _write(tmp_path, "alpha.py", _good("alpha"))

    result = discover_extensions(tmp_path)

    assert result.failures == ()
    assert [ext.meta.name for ext in result.loaded] == ["alpha"]
    extension = result.loaded[0]
    assert extension.meta.version == "1.0.0"
    assert extension.meta.description == "Test alpha."
    assert extension.run({"x": 1}) == {"echo": {"x": 1}}


def test_missing_folder_returns_empty_result(tmp_path: Path) -> None:
    result = discover_extensions(tmp_path)

    assert result.loaded == ()
    assert result.failures == ()


def test_loads_in_sorted_filename_order(tmp_path: Path) -> None:
    _write(tmp_path, "zeta.py", _good("zeta"))
    _write(tmp_path, "alpha.py", _good("alpha"))

    result = discover_extensions(tmp_path)

    assert [ext.meta.name for ext in result.loaded] == ["alpha", "zeta"]


def test_underscore_files_are_skipped_without_import(tmp_path: Path) -> None:
    _write(tmp_path, "_helper.py", "raise RuntimeError('must not be imported')\n")

    result = discover_extensions(tmp_path)

    assert result.loaded == ()
    assert result.failures == ()


def test_non_python_files_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "notes.txt", "not code")

    result = discover_extensions(tmp_path)

    assert result.loaded == ()
    assert result.failures == ()


def test_extensions_are_namespace_isolated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "one.py",
        'EXTENSION_META = {"name": "one", "version": "1.0.0", "description": "One."}\n'
        'SECRET = "one"\n\ndef run(payload):\n    return {"secret": SECRET}\n',
    )
    _write(
        tmp_path,
        "two.py",
        'EXTENSION_META = {"name": "two", "version": "1.0.0", "description": "Two."}\n'
        'SECRET = "two"\n\ndef run(payload):\n    return {"secret": SECRET}\n',
    )

    result = discover_extensions(tmp_path)

    by_name = {ext.meta.name: ext for ext in result.loaded}
    assert by_name["one"].run({}) == {"secret": "one"}
    assert by_name["two"].run({}) == {"secret": "two"}
    assert "SECRET" not in globals()
    assert by_name["one"].module_name != by_name["two"].module_name


@pytest.mark.parametrize(
    ("filename", "body", "expected"),
    [
        ("nometa.py", "def run(payload):\n    return {}\n", "must be a dict"),
        (
            "badname.py",
            (
                'EXTENSION_META = {"name": "Bad-Name", "version": "1.0.0", "description": "x"}\n'
                "def run(payload):\n    return {}\n"
            ),
            "invalid extension name",
        ),
        (
            "badversion.py",
            (
                'EXTENSION_META = {"name": "badver", "version": "1.0", "description": "x"}\n'
                "def run(payload):\n    return {}\n"
            ),
            "invalid extension version",
        ),
        (
            "nodesc.py",
            (
                'EXTENSION_META = {"name": "nodesc", "version": "1.0.0"}\n'
                "def run(payload):\n    return {}\n"
            ),
            "missing keys",
        ),
        (
            "extra.py",
            (
                'EXTENSION_META = {"name": "extra", "version": "1.0.0", "description": "x", "x": 1}\n'
                "def run(payload):\n    return {}\n"
            ),
            "unknown keys",
        ),
        (
            "norun.py",
            'EXTENSION_META = {"name": "norun", "version": "1.0.0", "description": "x"}\n',
            "callable run()",
        ),
        (
            "notcallable.py",
            (
                'EXTENSION_META = {"name": "notcall", "version": "1.0.0", "description": "x"}\n'
                "run = 5\n"
            ),
            "callable run()",
        ),
        ("syntax.py", "def (\n", "failed to import"),
        ("boom.py", 'raise RuntimeError("boom")\n', "failed to import"),
        (
            "longdesc.py",
            'EXTENSION_META = {"name": "longdesc", "version": "1.0.0", "description": "'
            + "a" * 301
            + '"}\ndef run(payload):\n    return {}\n',
            "exceeds",
        ),
    ],
)
def test_invalid_extensions_are_reported_not_raised(
    tmp_path: Path, filename: str, body: str, expected: str
) -> None:
    before = _leaked_modules()
    _write(tmp_path, filename, body)

    result = discover_extensions(tmp_path)

    assert result.loaded == ()
    assert len(result.failures) == 1
    assert expected in result.failures[0].error
    assert _leaked_modules() == before


def test_duplicate_names_keep_first_and_report_second(tmp_path: Path) -> None:
    _write(tmp_path, "a_first.py", _good("dup"))
    _write(tmp_path, "b_second.py", _good("dup"))

    result = discover_extensions(tmp_path)

    assert [ext.meta.name for ext in result.loaded] == ["dup"]
    assert result.loaded[0].path.name == "a_first.py"
    assert len(result.failures) == 1
    assert "duplicate extension name: dup" in result.failures[0].error


def test_symlink_pointing_outside_folder_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text(_good("escape"), encoding="utf-8")
    folder = tmp_path / EXTENSIONS_DIRNAME
    folder.mkdir()
    os.symlink(outside, folder / "link.py")

    result = discover_extensions(tmp_path)

    assert result.loaded == ()
    assert "outside the extensions folder" in result.failures[0].error


def test_load_extension_rejects_path_outside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text(_good("outside"), encoding="utf-8")
    root = tmp_path / EXTENSIONS_DIRNAME
    root.mkdir()

    with pytest.raises(ExtensionError, match="outside the extensions folder"):
        load_extension(outside, root)


def test_validate_meta_accepts_exact_shape() -> None:
    meta = validate_meta(
        {"name": "ok_name", "version": "0.2.10", "description": "Fine."}
    )

    assert meta.name == "ok_name"
    assert meta.version == "0.2.10"


def test_validate_meta_rejects_blank_description() -> None:
    with pytest.raises(ExtensionError, match="non-empty"):
        validate_meta({"name": "blank", "version": "1.0.0", "description": "   "})
