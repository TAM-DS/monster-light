"""Immutable evidence for deterministic trade execution."""

from dataclasses import dataclass
from enum import Enum


class AuditOutcome(Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class AuditOrigin(Enum):
    DIRECT = "DIRECT"
    APPROVED_PROPOSAL = "APPROVED_PROPOSAL"


@dataclass(frozen=True)
class ExecutionAuditContext:
    """Evidence labels only; never proof of approval or execution authority."""

    origin: AuditOrigin = AuditOrigin.DIRECT
    proposal_id: str | None = None
    approval_id: str | None = None
    market_evidence_id: str | None = None

    def __post_init__(self) -> None:
        if self.origin is AuditOrigin.DIRECT:
            if any(value is not None for value in (
                self.proposal_id, self.approval_id, self.market_evidence_id,
            )):
                raise ValueError("Direct execution cannot claim proposal, approval, or market evidence provenance")
        elif self.origin is AuditOrigin.APPROVED_PROPOSAL:
            for value in (self.proposal_id, self.approval_id, self.market_evidence_id):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Proposal execution provenance requires all three identifiers")
        else:
            raise ValueError("Unsupported audit origin")


@dataclass(frozen=True)
class TradeAudit:
    audit_id: str
    timestamp: str
    origin: AuditOrigin
    portfolio_id: str
    side: str
    symbol: str
    quantity: str
    price: str
    outcome: AuditOutcome
    reason_code: str
    reason_message: str
    cash_before: str | None
    cash_after: str | None
    quantity_before: int | None
    quantity_after: int | None
    proposal_id: str | None = None
    approval_id: str | None = None
    market_evidence_id: str | None = None
