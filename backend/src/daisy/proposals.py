"""Staged, human-approved changes to workspace files.

Flow: propose (stores content and runs pre-flight) -> approve or reject by a named human
-> apply. ``apply`` is the only function that writes to the workspace, and it refuses to
run unless the proposal is APPROVED. Before writing, it snapshots every target with
``daisy.backup`` so a bad write can be reverted with ``rollback_snapshot``.

State is held in memory for the life of the sidecar process. Approvals do not persist
across restarts, so a restart always returns to a closed gate.
"""

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from daisy.backup import SnapshotError, create_snapshot, rollback_snapshot
from daisy.preflight import PreflightInputError, PreflightReport, run_preflight, validate_relpath

DEFAULT_BACKUP_DIRNAME: Final[str] = "backup"


class ProposalStatus(StrEnum):
    """Lifecycle of a proposal. Only PENDING can become APPROVED or REJECTED."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class ProposalError(Exception):
    """Raised when a proposal operation is not allowed."""


class PreflightRejectedError(ProposalError):
    """Raised when approval is attempted on a proposal whose pre-flight checks failed."""


class ApprovalRequiredError(ProposalError):
    """Raised when a write is attempted without an APPROVED status."""


@dataclass
class Proposal:
    """One staged change set and its decision history."""

    proposal_id: str
    changes: dict[str, str]
    report: PreflightReport
    status: ProposalStatus
    created_at: datetime
    decided_by: str | None = None
    snapshot_id: str | None = None
    error: str | None = None


class ProposalManager:
    """Stages proposals and enforces the approval gate before any write."""

    def __init__(self, workspace: Path, backup_root: Path | None = None) -> None:
        self._workspace = workspace.resolve()
        default_root = self._workspace / DEFAULT_BACKUP_DIRNAME
        self._backup_root = (backup_root if backup_root is not None else default_root).resolve()
        self._proposals: dict[str, Proposal] = {}

    def _target(self, raw: str) -> Path:
        """Resolve a proposed path, rejecting anything outside the workspace or in the store."""
        try:
            rel = validate_relpath(raw)
        except PreflightInputError as exc:
            raise ProposalError(str(exc)) from exc
        target = (self._workspace / rel).resolve()
        if not target.is_relative_to(self._workspace):
            raise ProposalError(f"path escapes the workspace: {raw!r}")
        if target.is_relative_to(self._backup_root):
            raise ProposalError(f"refusing to write into the backup store: {raw!r}")
        return target

    def propose(self, changes: Mapping[str, str]) -> Proposal:
        """Stage a change set and run pre-flight checks. Nothing is written."""
        if not changes:
            raise ProposalError("a proposal needs at least one change")
        targets = [self._target(raw) for raw in changes]
        if len(set(targets)) != len(targets):
            raise ProposalError("two changes resolve to the same file")
        try:
            report = run_preflight(changes)
        except PreflightInputError as exc:
            raise ProposalError(str(exc)) from exc
        proposal = Proposal(
            proposal_id=uuid.uuid4().hex,
            changes=dict(changes),
            report=report,
            status=ProposalStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def get(self, proposal_id: str) -> Proposal:
        """Return a proposal by id, or raise ProposalError if it is unknown."""
        try:
            return self._proposals[proposal_id]
        except KeyError:
            raise ProposalError(f"unknown proposal: {proposal_id}") from None

    def _decide(self, proposal_id: str, decided_by: str, status: ProposalStatus) -> Proposal:
        proposal = self.get(proposal_id)
        approver = decided_by.strip()
        if not approver:
            raise ProposalError("the decision must name the human who made it")
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalError(f"proposal is {proposal.status.value}, not pending")
        proposal.status = status
        proposal.decided_by = approver
        return proposal

    def approve(self, proposal_id: str, decided_by: str) -> Proposal:
        """Approve a pending proposal whose pre-flight checks passed."""
        proposal = self.get(proposal_id)
        if proposal.status is ProposalStatus.PENDING and not proposal.report.passed:
            raise PreflightRejectedError(
                "pre-flight checks failed; the proposal cannot be approved"
            )
        return self._decide(proposal_id, decided_by, ProposalStatus.APPROVED)

    def reject(self, proposal_id: str, decided_by: str) -> Proposal:
        """Reject a pending proposal. A rejected proposal can never be applied."""
        return self._decide(proposal_id, decided_by, ProposalStatus.REJECTED)

    def apply(self, proposal_id: str) -> Proposal:
        """Write an approved proposal to the workspace, after snapshotting its targets.

        This is the only write path. Any other status raises ApprovalRequiredError before
        anything is touched. A failed write is rolled back from the snapshot.
        """
        proposal = self.get(proposal_id)
        if proposal.status is not ProposalStatus.APPROVED:
            raise ApprovalRequiredError(
                f"proposal {proposal_id} is {proposal.status.value}; "
                "human approval is required before any write"
            )
        targets = {raw: self._target(raw) for raw in proposal.changes}
        try:
            snapshot = create_snapshot(
                list(targets.values()),
                action_id=proposal.proposal_id,
                root=self._backup_root,
                workspace=self._workspace,
            )
        except SnapshotError as exc:
            raise ProposalError(f"could not snapshot targets: {exc}") from exc
        temps: list[Path] = []
        try:
            for raw, target in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.{proposal.proposal_id}.tmp")
                temps.append(temp)
                temp.write_bytes(proposal.changes[raw].encode("utf-8"))
                os.replace(temp, target)
        except OSError as exc:
            for temp in temps:
                temp.unlink(missing_ok=True)
            rollback_snapshot(snapshot.snapshot_id, self._backup_root, self._workspace)
            proposal.status = ProposalStatus.FAILED
            proposal.snapshot_id = snapshot.snapshot_id
            proposal.error = str(exc)
            raise ProposalError(
                f"write failed and files were restored from snapshot {snapshot.snapshot_id}: {exc}"
            ) from exc
        proposal.snapshot_id = snapshot.snapshot_id
        proposal.status = ProposalStatus.APPLIED
        return proposal
