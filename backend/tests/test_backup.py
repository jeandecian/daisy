"""Tests for daisy.backup snapshot and rollback, and the /health endpoint."""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daisy.backup import (
    METADATA_FILENAME,
    SNAPSHOT_ID_PATTERN,
    SnapshotError,
    SnapshotIntegrityError,
    create_snapshot,
    list_snapshots,
    latest_snapshot_id,
    rollback_snapshot,
    sha256_file,
)
from daisy.main import create_app, health_payload


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    return ws


@pytest.fixture
def store(workspace: Path) -> Path:
    return workspace / "backup"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# Snapshot creation and metadata


def test_snapshot_records_metadata_and_copies(workspace: Path, store: Path) -> None:
    target = _write(workspace / "notes.txt", "original")

    metadata = create_snapshot([target], action_id="act-1", root=store, workspace=workspace)

    assert SNAPSHOT_ID_PATTERN.fullmatch(metadata.snapshot_id)
    assert metadata.snapshot_id.endswith("_act-1")
    assert metadata.action_id == "act-1"
    assert metadata.timestamp.tzinfo is not None
    assert len(metadata.files) == 1
    entry = metadata.files[0]
    assert entry.original_path == str(target)
    assert entry.backup_name == "0000.bak"
    assert entry.existed is True
    assert entry.sha256 == sha256_file(target)

    snapshot_dir = store / metadata.snapshot_id
    assert (snapshot_dir / "0000.bak").read_text(encoding="utf-8") == "original"
    on_disk = json.loads((snapshot_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["snapshot_id"] == metadata.snapshot_id
    assert on_disk["files"][0]["sha256"] == entry.sha256


def test_missing_file_is_recorded_as_not_existed(workspace: Path, store: Path) -> None:
    absent = workspace / "new.txt"

    metadata = create_snapshot([absent], action_id="act-2", root=store, workspace=workspace)

    entry = metadata.files[0]
    assert entry.existed is False
    assert entry.sha256 is None


def test_duplicate_paths_are_recorded_once(workspace: Path, store: Path) -> None:
    target = _write(workspace / "a.txt", "x")

    metadata = create_snapshot([target, target], action_id="act-3", root=store, workspace=workspace)

    assert len(metadata.files) == 1


def test_invalid_action_id_is_rejected(workspace: Path, store: Path) -> None:
    target = _write(workspace / "a.txt", "x")

    with pytest.raises(ValueError):
        create_snapshot([target], action_id="bad id!", root=store, workspace=workspace)


def test_empty_file_list_is_rejected(workspace: Path, store: Path) -> None:
    with pytest.raises(ValueError):
        create_snapshot([], action_id="act-4", root=store, workspace=workspace)


# Listing


def test_list_returns_snapshots_newest_first(workspace: Path, store: Path) -> None:
    target = _write(workspace / "a.txt", "x")
    first = create_snapshot([target], action_id="first", root=store, workspace=workspace)
    time.sleep(0.01)
    second = create_snapshot([target], action_id="second", root=store, workspace=workspace)

    listing = list_snapshots(store, workspace)

    assert [item.snapshot_id for item in listing] == [second.snapshot_id, first.snapshot_id]
    assert latest_snapshot_id(store, workspace) == second.snapshot_id


def test_list_skips_unrelated_and_corrupt_directories(workspace: Path, store: Path) -> None:
    target = _write(workspace / "a.txt", "x")
    good = create_snapshot([target], action_id="good", root=store, workspace=workspace)
    (store / "not-a-snapshot").mkdir()
    corrupt = store / "20260101T000000000000Z_broken"
    corrupt.mkdir()
    (corrupt / METADATA_FILENAME).write_text("{ not json", encoding="utf-8")

    listing = list_snapshots(store, workspace)

    assert [item.snapshot_id for item in listing] == [good.snapshot_id]


def test_latest_is_none_when_store_is_empty(workspace: Path, store: Path) -> None:
    assert latest_snapshot_id(store, workspace) is None


# Path safety


def test_rejects_path_outside_workspace(tmp_path: Path, workspace: Path, store: Path) -> None:
    outside = _write(tmp_path / "outside.txt", "secret")

    with pytest.raises(SnapshotError, match="outside the workspace"):
        create_snapshot([outside], action_id="act-out", root=store, workspace=workspace)


def test_rejects_parent_traversal_out_of_workspace(
    tmp_path: Path, workspace: Path, store: Path
) -> None:
    _write(tmp_path / "outside.txt", "secret")
    traversal = workspace / ".." / "outside.txt"

    with pytest.raises(SnapshotError, match="outside the workspace"):
        create_snapshot([traversal], action_id="act-trav", root=store, workspace=workspace)


def test_rejects_targets_inside_backup_store(workspace: Path, store: Path) -> None:
    inside = _write(store / "x.txt", "meta")

    with pytest.raises(SnapshotError, match="backup store"):
        create_snapshot([inside], action_id="act-store", root=store, workspace=workspace)


def test_rejects_directory_target(workspace: Path, store: Path) -> None:
    folder = workspace / "sub"
    folder.mkdir()

    with pytest.raises(SnapshotError, match="not a regular file"):
        create_snapshot([folder], action_id="act-dir", root=store, workspace=workspace)


def test_rollback_rejects_tampered_metadata_path(
    tmp_path: Path, workspace: Path, store: Path
) -> None:
    target = _write(workspace / "a.txt", "original")
    metadata = create_snapshot([target], action_id="act-tamper", root=store, workspace=workspace)
    outside = _write(tmp_path / "outside.txt", "keep me")
    meta_path = store / metadata.snapshot_id / METADATA_FILENAME
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["files"][0]["original_path"] = str(outside)
    meta_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SnapshotError, match="outside the workspace"):
        rollback_snapshot(metadata.snapshot_id, store, workspace)

    assert outside.read_text(encoding="utf-8") == "keep me"


# Rollback and SHA-256 verification


def test_rollback_restores_original_content(workspace: Path, store: Path) -> None:
    target = _write(workspace / "a.txt", "original")
    metadata = create_snapshot([target], action_id="act-r1", root=store, workspace=workspace)
    target.write_text("changed by a bad edit", encoding="utf-8")

    restored = rollback_snapshot(metadata.snapshot_id, store, workspace)

    assert restored is True
    assert target.read_text(encoding="utf-8") == "original"


def test_rollback_removes_file_created_after_snapshot(workspace: Path, store: Path) -> None:
    created = workspace / "new.txt"
    metadata = create_snapshot([created], action_id="act-r2", root=store, workspace=workspace)
    created.write_text("written later", encoding="utf-8")

    rollback_snapshot(metadata.snapshot_id, store, workspace)

    assert not created.exists()


def test_rollback_detects_tampered_backup_and_changes_nothing(
    workspace: Path, store: Path
) -> None:
    first = _write(workspace / "first.txt", "one")
    second = _write(workspace / "second.txt", "two")
    metadata = create_snapshot(
        [first, second], action_id="act-r3", root=store, workspace=workspace
    )
    first.write_text("edited after snapshot", encoding="utf-8")
    (store / metadata.snapshot_id / "0001.bak").write_text("corrupted", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
        rollback_snapshot(metadata.snapshot_id, store, workspace)

    # Nothing was written: the first file keeps its post-snapshot edit.
    assert first.read_text(encoding="utf-8") == "edited after snapshot"
    assert second.read_text(encoding="utf-8") == "two"


def test_rollback_unknown_snapshot_returns_false(workspace: Path, store: Path) -> None:
    assert rollback_snapshot("20260101T000000000000Z_none", store, workspace) is False


def test_rollback_rejects_malformed_snapshot_id(workspace: Path, store: Path) -> None:
    assert rollback_snapshot("../../etc", store, workspace) is False


# Health endpoint


def test_health_payload_is_ok_with_iso_utc_timestamp() -> None:
    fixed = datetime(2026, 11, 26, 22, 35, 37, tzinfo=UTC)

    payload = health_payload(fixed)

    assert payload == {"status": "ok", "timestamp": "2026-11-26T22:35:37+00:00"}


def test_health_endpoint_returns_200_with_utc_timestamp(tmp_path: Path) -> None:
    app = create_app("test-token", db_path=tmp_path / "chat.sqlite")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    parsed = datetime.fromisoformat(body["timestamp"])
    assert parsed.utcoffset() == UTC.utcoffset(None)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00", body["timestamp"])
