"""Application trade boundary exercised against temporary SQLite databases."""

import sqlite3
from decimal import Decimal
from unittest.mock import Mock

import pytest

from monster_light.application.audit import AuditOrigin, ExecutionAuditContext
from monster_light.application.trade_service import TradeRequest, TradeService, TradeSide
from monster_light.domain.portfolio import (
    InsufficientCash, InsufficientShares, InvalidMoney, InvalidQuantity,
    InvalidSymbol, Portfolio, PositionNotFound,
)
from monster_light.infrastructure.sqlite_repository import (
    PortfolioNotFound, SQLitePortfolioRepository,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "trades.sqlite"
    connection = sqlite3.connect(path)
    repository = SQLitePortfolioRepository(connection)
    repository.initialize_schema()
    repository.save("one", Portfolio.restore(Decimal("80"), {"AAPL": 2}))
    try:
        yield path, connection, repository
    finally:
        connection.close()


@pytest.mark.parametrize("side, quantity, cash, positions", [
    (TradeSide.BUY, 3, "49.25", {"AAPL": 5}),
    (TradeSide.SELL, 1, "90.25", {"AAPL": 1}),
    (TradeSide.SELL, 2, "100.50", {}),
])
def test_trade_loads_authoritative_state_and_persists(
    database, side, quantity, cash, positions,
):
    path, connection, repository = database
    service = TradeService(repository)
    # A local snapshot is not authoritative input to execution.
    detached = repository.load("one")
    detached.buy("MSFT", 1, Decimal("1"))

    result = service.execute(TradeRequest("one", side, " aapl ", quantity, Decimal("10.25")))
    assert isinstance(result, Portfolio)
    assert result.cash == Decimal(cash)
    assert result.positions == positions
    persisted = repository.load("one")
    assert persisted.cash == result.cash
    assert persisted.positions == result.positions

    connection.close()
    reopened = sqlite3.connect(path)
    try:
        persisted = SQLitePortfolioRepository(reopened).load("one")
        assert persisted.cash == Decimal(cash)
        assert persisted.positions == positions
    finally:
        reopened.close()


@pytest.mark.parametrize("side, symbol, quantity, price, error", [
    (TradeSide.SELL, "AAPL", 3, Decimal("10"), InsufficientShares),
    (TradeSide.BUY, "AAPL", 9, Decimal("10"), InsufficientCash),
    (TradeSide.SELL, "MSFT", 1, Decimal("10"), PositionNotFound),
    (TradeSide.BUY, " ", 1, Decimal("10"), InvalidSymbol),
    (TradeSide.SELL, " ", 1, Decimal("10"), InvalidSymbol),
    (TradeSide.BUY, "AAPL", True, Decimal("10"), InvalidQuantity),
    (TradeSide.SELL, "AAPL", 0, Decimal("10"), InvalidQuantity),
    (TradeSide.BUY, "AAPL", 1, Decimal("NaN"), InvalidMoney),
    (TradeSide.SELL, "AAPL", 1, Decimal("-1"), InvalidMoney),
])
@pytest.mark.parametrize("context", [ExecutionAuditContext(), ExecutionAuditContext(
    AuditOrigin.APPROVED_PROPOSAL, "unverified-proposal", "unverified-approval")])
def test_domain_rejection_never_saves(database, monkeypatch, side, symbol, quantity, price, error, context):
    path, _, repository = database
    save = Mock(wraps=repository.save)
    monkeypatch.setattr(repository, "save", save)
    with pytest.raises(error):
        TradeService(repository).execute(TradeRequest("one", side, symbol, quantity, price), audit_context=context)
    save.assert_not_called()
    persisted = repository.load("one")
    assert persisted.cash == Decimal("80")
    assert persisted.positions == {"AAPL": 2}
    reopened = sqlite3.connect(path)
    try:
        persisted = SQLitePortfolioRepository(reopened).load("one")
        assert persisted.cash == Decimal("80")
        assert persisted.positions == {"AAPL": 2}
    finally:
        reopened.close()


def test_unknown_portfolio_remains_explicit(database, monkeypatch):
    _, _, repository = database
    save = Mock(wraps=repository.save)
    monkeypatch.setattr(repository, "save", save)
    with pytest.raises(PortfolioNotFound):
        TradeService(repository).execute(
            TradeRequest("missing", TradeSide.BUY, "AAPL", 1, Decimal("1"))
        )
    save.assert_not_called()
    with pytest.raises(PortfolioNotFound):
        repository.load("missing")


@pytest.mark.parametrize("portfolio_id", [None, 1, True, [], {}, "", " ", "\t\n"])
def test_invalid_identifier_rejected_before_repository_access(database, portfolio_id):
    _, _, repository = database
    observed = Mock(wraps=repository)
    with pytest.raises(ValueError, match="Portfolio identifier"):
        TradeService(observed).execute(
            TradeRequest(portfolio_id, TradeSide.BUY, "AAPL", 1, Decimal("1"))
        )
    assert observed.mock_calls == []


@pytest.mark.parametrize("side", ["BUY", "SELL", "HOLD", "buy", None, 1])
@pytest.mark.parametrize("context", [ExecutionAuditContext(), ExecutionAuditContext(
    AuditOrigin.APPROVED_PROPOSAL, "unverified-proposal", "unverified-approval")])
def test_only_explicit_trade_sides_are_accepted(database, side, context):
    _, _, repository = database
    observed = Mock(wraps=repository)
    with pytest.raises(ValueError, match="Trade side"):
        TradeService(observed).execute(TradeRequest("one", side, "AAPL", 1, Decimal("1")), audit_context=context)
    assert observed.mock_calls == []


def test_identifier_is_not_silently_trimmed(database):
    _, _, repository = database
    service = TradeService(repository)
    request = TradeRequest(" one ", TradeSide.BUY, "MSFT", 1, Decimal("5"))
    with pytest.raises(PortfolioNotFound):
        service.execute(request)
    repository.save(" one ", Portfolio(Decimal("20")))
    result = service.execute(request)
    assert result.cash == Decimal("15")
    assert result.positions == {"MSFT": 1}
    assert repository.load(" one ").cash == Decimal("15")
    assert repository.load("one").cash == Decimal("80")
    assert repository.load("one").positions == {"AAPL": 2}
