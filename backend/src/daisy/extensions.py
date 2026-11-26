"""Dynamic extension loader for the workspace extensions/ folder.

Every top-level ``*.py`` file in ``<workspace>/extensions`` whose name does not start
with an underscore is an extension. An extension must define a module-level
``EXTENSION_META`` dict with exactly the keys ``name``, ``version`` and ``description``,
and a callable ``run(payload)``.

Isolation: each extension runs as its own module under a unique, hash-derived name, so
module globals never collide between extensions and nothing is imported into the daisy
namespace. This is namespace isolation, not a security sandbox. Extension code runs with
the sidecar's privileges, so only load extensions that have passed the human approval
gate in ``daisy.proposals``.
"""

import hashlib
import importlib.util
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

EXTENSIONS_DIRNAME: Final[str] = "extensions"
META_ATTRIBUTE: Final[str] = "EXTENSION_META"
ENTRYPOINT_NAME: Final[str] = "run"
REQUIRED_META_KEYS: Final[frozenset[str]] = frozenset({"name", "version", "description"})
NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")
VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+")
MAX_DESCRIPTION_CHARS: Final[int] = 300
MODULE_PREFIX: Final[str] = "_daisy_ext_"

Entrypoint = Callable[[dict[str, Any]], dict[str, Any]]


class ExtensionError(Exception):
    """Raised when an extension is invalid or cannot be loaded."""


@dataclass(frozen=True)
class ExtensionMeta:
    """Validated metadata declared by an extension."""

    name: str
    version: str
    description: str


@dataclass(frozen=True)
class LoadedExtension:
    """An extension that imported and passed validation."""

    meta: ExtensionMeta
    path: Path
    module_name: str
    run: Entrypoint


@dataclass(frozen=True)
class LoadFailure:
    """A candidate file that was rejected, with the reason."""

    path: Path
    error: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of scanning the extensions folder."""

    loaded: tuple[LoadedExtension, ...]
    failures: tuple[LoadFailure, ...]


def validate_meta(raw: object) -> ExtensionMeta:
    """Check an extension's metadata dict and return it as a typed object."""
    if not isinstance(raw, dict):
        raise ExtensionError(f"{META_ATTRIBUTE} must be a dict")
    keys = set(raw)
    missing = REQUIRED_META_KEYS - keys
    if missing:
        raise ExtensionError(f"{META_ATTRIBUTE} is missing keys: {sorted(missing)}")
    unknown = keys - REQUIRED_META_KEYS
    if unknown:
        raise ExtensionError(f"{META_ATTRIBUTE} has unknown keys: {sorted(unknown)}")
    name = raw["name"]
    version = raw["version"]
    description = raw["description"]
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise ExtensionError(f"invalid extension name: {name!r}")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise ExtensionError(f"invalid extension version: {version!r}")
    if not isinstance(description, str) or not description.strip():
        raise ExtensionError("extension description must be a non-empty string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ExtensionError(f"extension description exceeds {MAX_DESCRIPTION_CHARS} characters")
    return ExtensionMeta(name=name, version=version, description=description)


def _module_name_for(path: Path) -> str:
    """Return a unique module name derived from the resolved file path."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"{MODULE_PREFIX}{digest}"


def load_extension(path: Path, extensions_root: Path) -> LoadedExtension:
    """Import one extension file and validate its metadata and entrypoint.

    The resolved file must sit inside ``extensions_root``, so a symlink cannot point an
    extension at code elsewhere. On any failure the module is removed from ``sys.modules``.
    """
    root = extensions_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ExtensionError(f"extension is outside the extensions folder: {resolved}")
    if not resolved.is_file() or resolved.suffix != ".py":
        raise ExtensionError(f"not a Python file: {resolved.name}")
    module_name = _module_name_for(resolved)
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ExtensionError(f"cannot create an import spec for {resolved.name}")
    module = importlib.util.module_from_spec(spec)
    # Registered under a unique name so dataclasses and pickling inside the extension work.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        raise ExtensionError(
            f"{resolved.name} failed to import: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        meta = validate_meta(getattr(module, META_ATTRIBUTE, None))
        run = getattr(module, ENTRYPOINT_NAME, None)
        if not callable(run):
            raise ExtensionError(f"extension must define a callable {ENTRYPOINT_NAME}()")
    except ExtensionError:
        sys.modules.pop(module_name, None)
        raise
    return LoadedExtension(meta=meta, path=resolved, module_name=module_name, run=run)


def discover_extensions(workspace: Path) -> DiscoveryResult:
    """Load every valid extension in ``<workspace>/extensions``.

    Rejected files are reported in ``failures`` and never raise, so one broken extension
    cannot stop the others from loading. Duplicate names keep the first file in sorted order.
    A missing folder yields an empty result.
    """
    root = (workspace / EXTENSIONS_DIRNAME).resolve()
    if not root.is_dir():
        return DiscoveryResult(loaded=(), failures=())
    loaded: list[LoadedExtension] = []
    failures: list[LoadFailure] = []
    seen: set[str] = set()
    for candidate in sorted(root.glob("*.py")):
        if candidate.name.startswith("_"):
            continue
        try:
            extension = load_extension(candidate, root)
        except ExtensionError as exc:
            failures.append(LoadFailure(path=candidate, error=str(exc)))
            continue
        if extension.meta.name in seen:
            sys.modules.pop(extension.module_name, None)
            failures.append(
                LoadFailure(
                    path=candidate,
                    error=f"duplicate extension name: {extension.meta.name}",
                )
            )
            continue
        seen.add(extension.meta.name)
        loaded.append(extension)
    return DiscoveryResult(loaded=tuple(loaded), failures=tuple(failures))
