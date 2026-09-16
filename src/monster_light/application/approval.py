"""Human approval evidence and coordination; approval does not execute a trade."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from monster_light.application.proposal import (
    ProposalAlreadyApproved, ProposalAlreadyRejected, ProposalStatus,
)
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint

if TYPE_CHECKING:
    from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository


def _validate_nonempty_string(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class HumanApproval:
    """Caller-supplied approver metadata, without authentication or verification."""

    proposal_id: str
    approver: str
    approval_id: str = field(default_factory=lambda: str(uuid4()))
    approved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("approval_id", "proposal_id", "approver"):
            _validate_nonempty_string(getattr(self, name), name)
        if (not isinstance(self.approved_at, datetime)
                or self.approved_at.utcoffset() != timedelta(0)):
            raise ValueError("approved_at must be a UTC timestamp")


class ApprovalService:
    """Atomically record approval, preserving ownership of outer transactions."""

    def __init__(self, repository: "SQLiteProposalRepository") -> None:
        self._repository = repository

    def approve(self, proposal_id: str, approver: str) -> HumanApproval:
        # Local import avoids a cycle with the repository's record type.
        from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository

        _validate_nonempty_string(approver, "approver")
        connection = self._repository.connection
        with sqlite_savepoint(connection):
            proposal = self._repository.load(proposal_id)
            if proposal.status is ProposalStatus.REJECTED:
                raise ProposalAlreadyRejected(proposal_id)
            if proposal.status is ProposalStatus.APPROVED:
                raise ProposalAlreadyApproved(proposal_id)
            approval = HumanApproval(proposal_id=proposal.proposal_id, approver=approver)
            approvals = SQLiteApprovalRepository(connection)
            approvals.initialize_schema()
            self._repository.mark_approved(proposal_id)
            approvals.append(approval)
        return approval
