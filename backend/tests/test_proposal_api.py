"""HTTP tests for the proposal lifecycle and extensions endpoints."""

import importlib.util
from pathlib import Path

import pytest
from daisy.extensions import EXTENSIONS_DIRNAME
from daisy.main import create_app
from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UNKNOWN_ID = "0" * 32

requires_tools = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None
    or importlib.util.find_spec("mypy") is None,
    reason="ruff and mypy are dev dependencies",
)

CLEAN = "def add(a: int, b: int) -> int:\n    return a + b\n"
BROKEN = "import os\nx=1\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def client(tmp_path: Path, workspace: Path) -> TestClient:
    app = create_app(TOKEN, db_path=tmp_path / "chat.sqlite", workspace=workspace)
    return TestClient(app)


def _propose(client: TestClient, changes: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/proposals/propose", json={"changes": changes}, headers=AUTH
    )
    assert response.status_code == 200
    return response.json()


def test_requests_without_token_are_rejected(client: TestClient) -> None:
    response = client.post("/proposals/propose", json={"changes": {"a.txt": "x"}})

    assert response.status_code == 401


def test_propose_returns_pending_with_report(
    client: TestClient, workspace: Path
) -> None:
    body = _propose(client, {"notes.txt": "hello\n"})

    assert body["status"] == "pending"
    assert body["paths"] == ["notes.txt"]
    assert body["preflight"]["passed"] is True  # type: ignore[index]
    assert body["preflight"]["checks"] == []  # type: ignore[index]
    assert not (workspace / "notes.txt").exists()


def test_propose_rejects_unsafe_path(client: TestClient) -> None:
    response = client.post(
        "/proposals/propose", json={"changes": {"../evil.txt": "x"}}, headers=AUTH
    )

    assert response.status_code == 400


def test_propose_rejects_empty_changes(client: TestClient) -> None:
    response = client.post("/proposals/propose", json={"changes": {}}, headers=AUTH)

    assert response.status_code == 400


def test_approve_then_apply_writes_file(client: TestClient, workspace: Path) -> None:
    proposal_id = _propose(client, {"notes/todo.txt": "hello\n"})["proposal_id"]

    approved = client.post(
        f"/proposals/{proposal_id}/approve", json={"approver": "jean"}, headers=AUTH
    )
    applied = client.post(f"/proposals/{proposal_id}/apply", headers=AUTH)

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == "jean"
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert applied.json()["snapshot_id"]
    assert (workspace / "notes" / "todo.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_before_approval_is_refused(client: TestClient, workspace: Path) -> None:
    proposal_id = _propose(client, {"notes.txt": "hello\n"})["proposal_id"]

    response = client.post(f"/proposals/{proposal_id}/apply", headers=AUTH)

    assert response.status_code == 409
    assert not (workspace / "notes.txt").exists()


def test_rejected_proposal_cannot_be_applied(
    client: TestClient, workspace: Path
) -> None:
    proposal_id = _propose(client, {"notes.txt": "hello\n"})["proposal_id"]

    rejected = client.post(
        f"/proposals/{proposal_id}/reject", json={"approver": "jean"}, headers=AUTH
    )
    response = client.post(f"/proposals/{proposal_id}/apply", headers=AUTH)

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert response.status_code == 409
    assert not (workspace / "notes.txt").exists()


def test_blank_approver_is_refused(client: TestClient) -> None:
    proposal_id = _propose(client, {"notes.txt": "hello\n"})["proposal_id"]

    response = client.post(
        f"/proposals/{proposal_id}/approve", json={"approver": "   "}, headers=AUTH
    )

    assert response.status_code == 409


def test_unknown_proposal_is_404(client: TestClient) -> None:
    approve = client.post(
        f"/proposals/{UNKNOWN_ID}/approve", json={"approver": "jean"}, headers=AUTH
    )
    reject = client.post(
        f"/proposals/{UNKNOWN_ID}/reject", json={"approver": "jean"}, headers=AUTH
    )
    apply = client.post(f"/proposals/{UNKNOWN_ID}/apply", headers=AUTH)

    assert approve.status_code == 404
    assert reject.status_code == 404
    assert apply.status_code == 404


@requires_tools
def test_failed_preflight_cannot_be_approved(client: TestClient) -> None:
    body = _propose(client, {"mod.py": BROKEN})
    proposal_id = body["proposal_id"]

    response = client.post(
        f"/proposals/{proposal_id}/approve", json={"approver": "jean"}, headers=AUTH
    )

    assert body["preflight"]["passed"] is False  # type: ignore[index]
    assert response.status_code == 409


@requires_tools
def test_clean_python_proposal_reports_all_checks(client: TestClient) -> None:
    body = _propose(client, {"mod.py": CLEAN})

    names = [check["name"] for check in body["preflight"]["checks"]]  # type: ignore[index]
    assert names == ["ruff_check", "ruff_format", "mypy"]


def test_extensions_endpoint_reports_loaded_and_failed(
    tmp_path: Path, workspace: Path
) -> None:
    folder = workspace / EXTENSIONS_DIRNAME
    folder.mkdir()
    (folder / "good.py").write_text(
        'EXTENSION_META = {"name": "good", "version": "1.0.0", "description": "Good."}\n'
        "\ndef run(payload):\n    return {}\n",
        encoding="utf-8",
    )
    (folder / "bad.py").write_text('raise RuntimeError("boom")\n', encoding="utf-8")
    app = create_app(TOKEN, db_path=tmp_path / "chat.sqlite", workspace=workspace)

    response = TestClient(app).get("/extensions", headers=AUTH)

    assert response.status_code == 200
    items = {item["file"]: item for item in response.json()["extensions"]}
    assert items["good.py"]["status"] == "loaded"
    assert items["good.py"]["name"] == "good"
    assert items["bad.py"]["status"] == "failed"
    assert "boom" in items["bad.py"]["error"]
