"""Snapshot persistence and restoration invariants, using temporary databases."""

import sqlite3
from decimal import Decimal

import pytest

from monster_light.domain.portfolio import (
    InvalidMoney, InvalidQuantity, InvalidSymbol, Portfolio,
)
from monster_light.infrastructure.sqlite_repository import (
    PortfolioNotFound, SQLitePortfolioRepository,
)


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(tmp_path / "portfolio.sqlite")
    repository = SQLitePortfolioRepository(connection)
    repository.initialize_schema()
    try:
        yield connection, repository
    finally:
        connection.close()


def test_snapshot_round_trip(database):
    _, repository = database
    portfolio = Portfolio(Decimal("100.00"))
    portfolio.buy(" aapl ", 2, Decimal("10.25"))
    portfolio.buy("msft", 3, Decimal("5"))
    repository.save("one", portfolio)
    repository.initialize_schema()  # Initialization preserves existing state.
    loaded = repository.load("one")
    assert loaded.cash == portfolio.cash
    assert loaded.positions == portfolio.positions


def test_decimal_is_stored_as_exact_text(database):
    connection, repository = database
    cash = Decimal("12345678901234567890.123456789012345678900")
    repository.save("one", Portfolio(cash))
    assert connection.execute("SELECT cash, typeof(cash) FROM portfolios").fetchone() == (
        str(cash), "text",
    )
    assert repository.load("one").cash.as_tuple() == cash.as_tuple()


def test_sold_position_removed_and_portfolios_isolated(database):
    _, repository = database
    first = Portfolio(Decimal("100"))
    first.buy("AAPL", 2, Decimal("10"))
    second = Portfolio(Decimal("200"))
    second.buy("AAPL", 3, Decimal("10"))
    repository.save("one", first)
    repository.save("two", second)
    first.sell("AAPL", 2, Decimal("12"))
    repository.save("one", first)
    assert repository.load("one").positions == {}
    assert repository.load("one").cash == Decimal("104")
    assert repository.load("two").positions == {"AAPL": 3}
    assert repository.load("two").cash == Decimal("170")


def test_unknown_portfolio(database):
    _, repository = database
    with pytest.raises(PortfolioNotFound):
        repository.load("missing")


def test_survives_close_and_reopen(tmp_path):
    path = tmp_path / "durable.sqlite"
    connection = sqlite3.connect(path)
    try:
        repository = SQLitePortfolioRepository(connection)
        repository.initialize_schema()
        portfolio = Portfolio.restore(Decimal("0.0100"), {"AAPL": 7})
        repository.save("one", portfolio)
    finally:
        connection.close()
    reopened = sqlite3.connect(path)
    try:
        loaded = SQLitePortfolioRepository(reopened).load("one")
        assert loaded.cash.as_tuple() == portfolio.cash.as_tuple()
        assert loaded.positions == portfolio.positions
    finally:
        reopened.close()


@pytest.mark.parametrize("cash", ["-1", "NaN", "sNaN", "Infinity", "garbage"])
def test_invalid_persisted_cash_rejected(database, cash):
    connection, repository = database
    connection.execute("INSERT INTO portfolios VALUES (?, ?)", ("one", cash))
    with pytest.raises(InvalidMoney):
        repository.load("one")


@pytest.mark.parametrize("symbol", ["aapl", " AAPL ", "", " "])
def test_invalid_persisted_symbol_rejected(database, symbol):
    connection, repository = database
    repository.save("one", Portfolio(Decimal("10")))
    connection.execute("INSERT INTO positions VALUES (?, ?, ?)", ("one", symbol, 1))
    with pytest.raises(InvalidSymbol):
        repository.load("one")


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "bad"])
def test_invalid_persisted_quantity_rejected(database, quantity):
    connection, repository = database
    repository.save("one", Portfolio(Decimal("10")))
    # Simulate damaged data that bypassed database constraints.
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute("INSERT INTO positions VALUES (?, ?, ?)", ("one", "AAPL", quantity))
    with pytest.raises(InvalidQuantity):
        repository.load("one")


@pytest.mark.parametrize("quantity", [True, False, 0, -1, 1.5, "1", Decimal("1")])
def test_restore_rejects_non_positive_integer_quantities(quantity):
    with pytest.raises(InvalidQuantity):
        Portfolio.restore(Decimal("0"), {"AAPL": quantity})


@pytest.mark.parametrize("cash", [Decimal("-1"), Decimal("NaN"), 1.0, "1"])
def test_restore_validates_cash(cash):
    with pytest.raises(InvalidMoney):
        Portfolio.restore(cash, {})


def test_restore_copies_positions():
    positions = {"AAPL": 1}
    portfolio = Portfolio.restore(Decimal("0"), positions)
    positions["AAPL"] = 9
    assert portfolio.positions == {"AAPL": 1}
    with pytest.raises(TypeError):
        portfolio.positions["AAPL"] = 2


@pytest.mark.parametrize("isolation_level", [None, "DEFERRED"])
def test_failed_snapshot_write_rolls_back(database, isolation_level):
    connection, repository = database
    connection.isolation_level = isolation_level
    original = Portfolio.restore(Decimal("100"), {"AAPL": 1})
    repository.save("one", original)
    connection.execute("""
        CREATE TRIGGER reject_msft BEFORE INSERT ON positions
        WHEN NEW.symbol = 'MSFT'
        BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
    """)
    replacement = Portfolio.restore(Decimal("50"), {"AAPL": 2, "MSFT": 1})
    with pytest.raises(sqlite3.IntegrityError):
        repository.save("one", replacement)
    loaded = repository.load("one")
    assert loaded.cash == original.cash
    assert loaded.positions == original.positions


def test_save_respects_outer_transaction(database):
    connection, repository = database
    connection.execute("BEGIN")
    repository.save("one", Portfolio(Decimal("10")))
    connection.rollback()
    with pytest.raises(PortfolioNotFound):
        repository.load("one")
