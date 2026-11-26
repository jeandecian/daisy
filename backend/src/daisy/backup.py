"""Snapshot and rollback for files the sidecar is about to change.

A snapshot is a directory ``<backup_root>/<snapshot_id>/`` holding copies of the target
files (``0000.bak``, ``0001.bak``, ...) and ``metadata.json``. The snapshot id is
``<UTC timestamp>_<action_id>``.

Path safety: every target must resolve inside the workspace root and must not be inside
the backup store itself. Both checks run when a snapshot is created and again when it is
restored, because ``metadata.json`` is data on disk and can be edited. Rollback validates
all paths and all checksums before it writes anything.

Command line (used by the Tauri shell)::

    python -m daisy.backup create [--action-id ID] -- FILE...
    python -m daisy.backup rollback [SNAPSHOT_ID]   # defaults to the latest snapshot
    python -m daisy.backup list
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# backend/src/daisy/backup.py -> parents[3] is the project root.
DEFAULT_WORKSPACE: Final[Path] = Path(__file__).resolve().parents[3]
BACKUP_DIRNAME: Final[str] = "backup"
METADATA_FILENAME: Final[str] = "metadata.json"
ACTION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")
SNAPSHOT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\d{8}T\d{12}Z_[A-Za-z0-9_-]{1,64}"
)
BACKUP_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{4}\.bak")
TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%S%fZ"
CHUNK_SIZE: Final[int] = 1024 * 1024


class SnapshotError(Exception):
    """Raised when a snapshot cannot be created or read, or a path is not allowed."""


class SnapshotIntegrityError(SnapshotError):
    """Raised when a snapshot copy no longer matches its recorded checksum."""


class SnapshotFile(BaseModel):
    """One target file captured by a snapshot."""

    model_config = ConfigDict(frozen=True)

    original_path: str
    backup_name: str
    existed: bool
    sha256: str | None = None


class SnapshotMetadata(BaseModel):
    """Contents of ``metadata.json`` inside a snapshot directory."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    timestamp: datetime
    action_id: str
    files: list[SnapshotFile] = Field(default_factory=list)


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_workspace(workspace: Path | None) -> Path:
    """Return the resolved workspace root that every target must stay inside."""
    return (workspace if workspace is not None else DEFAULT_WORKSPACE).resolve()


def _backup_root(root: Path | None, workspace: Path) -> Path:
    """Resolve the snapshot store, creating it if needed. Defaults to ``<workspace>/backup``."""
    path = (root if root is not None else workspace / BACKUP_DIRNAME).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _checked_target(raw: Path, workspace: Path, backup_root: Path) -> Path:
    """Resolve a target path and reject anything outside the workspace or inside the store."""
    resolved = raw.resolve()
    if not resolved.is_relative_to(workspace):
        raise SnapshotError(f"path is outside the workspace: {resolved}")
    if resolved.is_relative_to(backup_root):
        raise SnapshotError(f"refusing to snapshot the backup store: {resolved}")
    return resolved


def _load_metadata(snapshot_dir: Path) -> SnapshotMetadata:
    """Read and validate the metadata file of one snapshot directory."""
    try:
        return SnapshotMetadata.model_validate_json(
            (snapshot_dir / METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"unreadable snapshot metadata in {snapshot_dir.name}") from exc


def create_snapshot(
    files: Sequence[Path],
    action_id: str | None = None,
    root: Path | None = None,
    workspace: Path | None = None,
) -> SnapshotMetadata:
    """Copy each file into a new snapshot directory and record its checksum.

    All paths are validated before anything is copied. A file that does not exist yet is
    recorded with ``existed=False`` so a rollback removes it. The snapshot is written to a
    staging directory and renamed into place, so an interrupted run never leaves a
    half-written snapshot behind.
    """
    if not files:
        raise ValueError("no files to snapshot")
    ws = _resolve_workspace(workspace)
    backup_root = _backup_root(root, ws)
    action = action_id if action_id is not None else uuid.uuid4().hex
    if ACTION_ID_PATTERN.fullmatch(action) is None:
        raise ValueError(f"invalid action_id: {action!r}")

    targets: list[Path] = []
    for source in files:
        target = _checked_target(source, ws, backup_root)
        if target not in targets:
            targets.append(target)
    for target in targets:
        if target.exists() and not target.is_file():
            raise SnapshotError(f"not a regular file: {target}")

    now = datetime.now(UTC)
    snapshot_id = f"{now.strftime(TIMESTAMP_FORMAT)}_{action}"
    staging = backup_root / f".staging-{snapshot_id}"
    staging.mkdir()
    try:
        entries: list[SnapshotFile] = []
        for index, target in enumerate(targets):
            name = f"{index:04d}.bak"
            if target.is_file():
                shutil.copy2(target, staging / name)
                entries.append(
                    SnapshotFile(
                        original_path=str(target),
                        backup_name=name,
                        existed=True,
                        sha256=sha256_file(staging / name),
                    )
                )
            else:
                entries.append(
                    SnapshotFile(
                        original_path=str(target),
                        backup_name=name,
                        existed=False,
                    )
                )
        metadata = SnapshotMetadata(
            snapshot_id=snapshot_id,
            timestamp=now,
            action_id=action,
            files=entries,
        )
        (staging / METADATA_FILENAME).write_text(
            metadata.model_dump_json(indent=2), encoding="utf-8"
        )
        os.replace(staging, backup_root / snapshot_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return metadata


def list_snapshots(
    root: Path | None = None,
    workspace: Path | None = None,
) -> list[SnapshotMetadata]:
    """Return every readable snapshot, newest first.

    Directories that do not match the snapshot naming scheme, and snapshots with
    unreadable metadata, are skipped rather than failing the whole listing.
    """
    backup_root = _backup_root(root, _resolve_workspace(workspace))
    snapshots: list[SnapshotMetadata] = []
    for entry in backup_root.iterdir():
        if not entry.is_dir() or SNAPSHOT_ID_PATTERN.fullmatch(entry.name) is None:
            continue
        try:
            snapshots.append(_load_metadata(entry))
        except SnapshotError:
            continue
    return sorted(snapshots, key=lambda item: item.snapshot_id, reverse=True)


def latest_snapshot_id(
    root: Path | None = None,
    workspace: Path | None = None,
) -> str | None:
    """Return the id of the newest snapshot, or None when there are none."""
    snapshots = list_snapshots(root, workspace)
    return snapshots[0].snapshot_id if snapshots else None


def rollback_snapshot(
    snapshot_id: str,
    root: Path | None = None,
    workspace: Path | None = None,
) -> bool:
    """Restore every file recorded in a snapshot.

    Returns False when the snapshot does not exist. Before writing anything, this checks
    that every recorded path is inside the workspace and not inside the store, that every
    backup name is well formed, and that every copy matches its SHA-256. Any failure
    leaves all targets untouched. Files that did not exist when the snapshot was taken
    are removed.
    """
    if SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
        return False
    ws = _resolve_workspace(workspace)
    backup_root = _backup_root(root, ws)
    snapshot_dir = backup_root / snapshot_id
    if not (snapshot_dir / METADATA_FILENAME).is_file():
        return False
    metadata = _load_metadata(snapshot_dir)

    plan: list[tuple[SnapshotFile, Path]] = []
    for entry in metadata.files:
        if BACKUP_NAME_PATTERN.fullmatch(entry.backup_name) is None:
            raise SnapshotIntegrityError(f"invalid backup name: {entry.backup_name!r}")
        target = _checked_target(Path(entry.original_path), ws, backup_root)
        if entry.existed:
            source = snapshot_dir / entry.backup_name
            if not source.is_file() or sha256_file(source) != entry.sha256:
                raise SnapshotIntegrityError(f"checksum mismatch for {entry.original_path}")
        plan.append((entry, target))

    for entry, target in plan:
        if entry.existed:
            temp = target.with_name(f".{target.name}.rollback-tmp")
            shutil.copy2(snapshot_dir / entry.backup_name, temp)
            os.replace(temp, target)
        elif target.is_file():
            target.unlink()
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the snapshot command line. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="daisy.backup", description="Snapshot and rollback.")
    parser.add_argument("--root", type=Path, default=None, help="Snapshot store directory.")
    parser.add_argument(
        "--workspace", type=Path, default=None, help="Root that all target paths must be inside."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Snapshot the given files.")
    create.add_argument("--action-id", default=None, help="Identifier stored in the snapshot.")
    create.add_argument("files", nargs="+", type=Path)

    rollback = commands.add_parser("rollback", help="Restore a snapshot.")
    rollback.add_argument("snapshot_id", nargs="?", default=None)

    commands.add_parser("list", help="List snapshots as JSON, newest first.")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            metadata = create_snapshot(args.files, args.action_id, args.root, args.workspace)
            print(metadata.snapshot_id, flush=True)
            return 0
        if args.command == "rollback":
            target_id: str | None = args.snapshot_id or latest_snapshot_id(
                args.root, args.workspace
            )
            if target_id is None:
                print("no snapshots to roll back to", file=sys.stderr)
                return 1
            if not rollback_snapshot(target_id, args.root, args.workspace):
                print(f"snapshot not found: {target_id}", file=sys.stderr)
                return 1
            print(target_id, flush=True)
            return 0
        listing = [
            item.model_dump(mode="json") for item in list_snapshots(args.root, args.workspace)
        ]
        print(json.dumps(listing, indent=2), flush=True)
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
