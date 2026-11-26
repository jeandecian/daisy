"""Tests for the staging and approval gate in daisy.proposals."""

import importlib.util
from pathlib import Path

import pytest

from daisy.backup import rollback_snapshot
from daisy.proposals import (
    ApprovalRequiredError,
    PreflightRejectedError,
    ProposalError,
    ProposalManager,
    ProposalStatus,
)

requires_tools = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None or importlib.util.find_spec("mypy") is None,
    reason="ruff and mypy are dev dependencies",
)

CLEAN = "def add(a: int, b: int) -> int:\n    return a + b\n"
BROKEN = "import os\nx=1\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@requires_tools
def test_propose_runs_preflight_and_stays_pending(workspace: Path) -> None:
    manager = ProposalManager(workspace)

    proposal = manager.propose({"mod.py": CLEAN})

    assert proposal.status is ProposalStatus.PENDING
    assert proposal.report.passed is True
    assert not (workspace / "mod.py").exists()


@requires_tools
def test_failed_preflight_cannot_be_approved(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"mod.py": BROKEN})

    assert proposal.report.passed is False
    with pytest.raises(PreflightRejectedError):
        manager.approve(proposal.proposal_id, "jean")
    assert manager.get(proposal.proposal_id).status is ProposalStatus.PENDING


@requires_tools
def test_apply_without_approval_writes_nothing(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"mod.py": CLEAN})

    with pytest.raises(ApprovalRequiredError):
        manager.apply(proposal.proposal_id)

    assert not (workspace / "mod.py").exists()


@requires_tools
def test_approve_then_apply_writes_content_and_snapshots(workspace: Path) -> None:
    (workspace / "mod.py").write_text("old = 0\n", encoding="utf-8")
    manager = ProposalManager(workspace)
    proposal = manager.propose({"mod.py": CLEAN})
    manager.approve(proposal.proposal_id, "jean")

    applied = manager.apply(proposal.proposal_id)

    assert applied.status is ProposalStatus.APPLIED
    assert applied.decided_by == "jean"
    assert (workspace / "mod.py").read_text(encoding="utf-8") == CLEAN
    assert applied.snapshot_id is not None
    assert (workspace / "backup" / applied.snapshot_id / "metadata.json").is_file()

    rollback_snapshot(applied.snapshot_id, workspace / "backup", workspace)
    assert (workspace / "mod.py").read_text(encoding="utf-8") == "old = 0\n"


def test_non_python_change_applies_after_approval(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"notes/todo.txt": "hello\n"})
    manager.approve(proposal.proposal_id, "jean")

    manager.apply(proposal.proposal_id)

    assert (workspace / "notes" / "todo.txt").read_text(encoding="utf-8") == "hello\n"


def test_rejected_proposal_can_never_be_applied(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"notes.txt": "hello\n"})
    manager.reject(proposal.proposal_id, "jean")

    with pytest.raises(ApprovalRequiredError):
        manager.apply(proposal.proposal_id)
    assert not (workspace / "notes.txt").exists()


def test_approval_requires_named_human(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"notes.txt": "hello\n"})

    with pytest.raises(ProposalError, match="name the human"):
        manager.approve(proposal.proposal_id, "   ")


def test_decision_is_final(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"notes.txt": "hello\n"})
    manager.approve(proposal.proposal_id, "jean")

    with pytest.raises(ProposalError, match="not pending"):
        manager.approve(proposal.proposal_id, "jean")
    with pytest.raises(ProposalError, match="not pending"):
        manager.reject(proposal.proposal_id, "jean")


def test_apply_twice_is_refused(workspace: Path) -> None:
    manager = ProposalManager(workspace)
    proposal = manager.propose({"notes.txt": "hello\n"})
    manager.approve(proposal.proposal_id, "jean")
    manager.apply(proposal.proposal_id)

    with pytest.raises(ApprovalRequiredError):
        manager.apply(proposal.proposal_id)


def test_propose_rejects_path_escaping_workspace(tmp_path: Path, workspace: Path) -> None:
    manager = ProposalManager(workspace)

    with pytest.raises(ProposalError, match="stay inside the workspace"):
        manager.propose({"../evil.txt": "x"})
    assert not (tmp_path / "evil.txt").exists()


def test_propose_rejects_writes_into_backup_store(workspace: Path) -> None:
    manager = ProposalManager(workspace)

    with pytest.raises(ProposalError, match="backup store"):
        manager.propose({"backup/x.txt": "x"})


def test_propose_rejects_two_changes_to_same_file(workspace: Path) -> None:
    manager = ProposalManager(workspace)

    with pytest.raises(ProposalError, match="same file"):
        manager.propose({"a.txt": "one", "./a.txt": "two"})


def test_propose_rejects_empty_change_set(workspace: Path) -> None:
    manager = ProposalManager(workspace)

    with pytest.raises(ProposalError, match="at least one change"):
        manager.propose({})


def test_unknown_proposal_id_raises(workspace: Path) -> None:
    manager = ProposalManager(workspace)

    with pytest.raises(ProposalError, match="unknown proposal"):
        manager.get("0" * 32)
