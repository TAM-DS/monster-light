"""Immutable evidence for direct deterministic trade execution."""

from dataclasses import dataclass
from enum import Enum


class AuditOutcome(Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class AuditOrigin(Enum):
    DIRECT = "DIRECT"


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
