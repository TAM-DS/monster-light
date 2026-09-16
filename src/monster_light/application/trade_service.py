"""Coordinate structured trades against authoritative SQLite snapshots."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from datetime import datetime, timezone
from uuid import uuid4

from monster_light.application.audit import AuditOutcome, ExecutionAuditContext, TradeAudit
from monster_light.domain.portfolio import Portfolio, PortfolioError
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint
from monster_light.infrastructure.sqlite_repository import PortfolioNotFound, SQLitePortfolioRepository


class TradeSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class TradeRequest:
    portfolio_id: str
    side: TradeSide
    symbol: str
    quantity: int
    price: Decimal


class TradeService:
    """Validate and atomically persist trades and their evidence.

    Callers own connection lifetime. A caller's outer transaction also owns the
    durability of audit evidence, including rejections. The savepoint provides
    atomicity for this operation. Concurrent-writer behavior relies on SQLite
    locking; explicit stale-write/version protection is not implemented.
    """

    def __init__(self, repository: SQLitePortfolioRepository) -> None:
        self._repository = repository

    def execute(
        self, request: TradeRequest, *, audit_context: ExecutionAuditContext = ExecutionAuditContext(),
    ) -> Portfolio:
        if not isinstance(request.portfolio_id, str) or not request.portfolio_id.strip():
            raise ValueError("Portfolio identifier must be a non-empty string")
        if not isinstance(request.side, TradeSide):
            raise ValueError("Trade side must be TradeSide.BUY or TradeSide.SELL")

        connection = self._repository.connection
        audits = SQLiteAuditRepository(connection)
        audits.initialize_schema()
        rejection = None
        with sqlite_savepoint(connection):
            portfolio = None
            cash_before = None
            quantity_before = None
            try:
                portfolio = self._repository.load(request.portfolio_id)
                cash_before = str(portfolio.cash)
                quantity_before = portfolio.quantity_for(request.symbol)
                if request.side is TradeSide.BUY:
                    portfolio.buy(request.symbol, request.quantity, request.price)
                else:
                    portfolio.sell(request.symbol, request.quantity, request.price)
            except (PortfolioNotFound, PortfolioError) as error:
                rejection = error

            if rejection is None:
                self._repository.save(request.portfolio_id, portfolio)
            audit = TradeAudit(
                audit_id=str(uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(),
                origin=audit_context.origin,
                proposal_id=audit_context.proposal_id,
                approval_id=audit_context.approval_id,
                market_evidence_id=audit_context.market_evidence_id,
                portfolio_id=request.portfolio_id,
                side=request.side.value,
                symbol=request.symbol if isinstance(request.symbol, str) else repr(request.symbol),
                quantity=str(request.quantity),
                price=str(request.price),
                outcome=AuditOutcome.REJECTED if rejection else AuditOutcome.ACCEPTED,
                reason_code=type(rejection).__name__ if rejection else "TradeExecuted",
                reason_message=str(rejection) if rejection else "Trade executed",
                cash_before=cash_before,
                cash_after=cash_before if rejection else str(portfolio.cash),
                quantity_before=quantity_before,
                quantity_after=quantity_before if rejection else portfolio.quantity_for(request.symbol),
            )
            try:
                audits.append(audit)
            except Exception as audit_error:
                if rejection is not None:
                    raise rejection from audit_error
                raise
        # Raise after releasing the savepoint so rejected evidence is retained.
        if rejection is not None:
            raise rejection
        return portfolio
