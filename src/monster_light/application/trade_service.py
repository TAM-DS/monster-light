"""Coordinate structured trades against authoritative SQLite snapshots."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


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
    """Load, apply domain rules, and save a trade.

    Callers own connection lifetime and must serialize trade execution; the
    repository's atomic save does not protect this read/modify/write sequence
    against concurrent writers. Existing caller transactions remain caller-owned.
    """

    def __init__(self, repository: SQLitePortfolioRepository) -> None:
        self._repository = repository

    def execute(self, request: TradeRequest) -> Portfolio:
        if not isinstance(request.portfolio_id, str) or not request.portfolio_id.strip():
            raise ValueError("Portfolio identifier must be a non-empty string")
        if not isinstance(request.side, TradeSide):
            raise ValueError("Trade side must be TradeSide.BUY or TradeSide.SELL")

        portfolio = self._repository.load(request.portfolio_id)
        if request.side is TradeSide.BUY:
            portfolio.buy(request.symbol, request.quantity, request.price)
        else:
            portfolio.sell(request.symbol, request.quantity, request.price)
        self._repository.save(request.portfolio_id, portfolio)
        return portfolio
