"""Candidate trade records, independent of execution and portfolio state."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from monster_light.application.trade_service import TradeSide


class ProposalOrigin(Enum):
    MANUAL = "MANUAL"


class ProposalStatus(Enum):
    PENDING = "PENDING"
    REJECTED = "REJECTED"


class ProposalNotFound(LookupError):
    """No proposal has the requested identifier."""


class ProposalAlreadyRejected(ValueError):
    """A rejected proposal cannot be rejected again."""


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class TradeProposal:
    portfolio_id: str
    side: TradeSide
    symbol: str
    quantity: int
    price: Decimal
    rationale: str
    proposal_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    origin: ProposalOrigin = ProposalOrigin.MANUAL
    status: ProposalStatus = ProposalStatus.PENDING
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("proposal_id", "portfolio_id", "symbol", "rationale"):
            _nonempty(getattr(self, name), name)
        if not isinstance(self.side, TradeSide):
            raise ValueError("side must be a TradeSide")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer, excluding booleans")
        if (not isinstance(self.price, Decimal) or not self.price.is_finite()
                or self.price <= 0):
            raise ValueError("price must be a positive finite Decimal")
        if self.origin is not ProposalOrigin.MANUAL:
            raise ValueError("origin must be ProposalOrigin.MANUAL")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be a UTC timestamp")
        if not isinstance(self.status, ProposalStatus):
            raise ValueError("status must be a ProposalStatus")
        if self.status is ProposalStatus.REJECTED:
            _nonempty(self.rejection_reason, "rejection_reason")
        elif self.rejection_reason is not None:
            raise ValueError("PENDING proposals cannot have a rejection reason")
