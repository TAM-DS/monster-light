"""SQLite-backed portfolio snapshots using a caller-owned connection."""

import sqlite3
from decimal import Decimal, DecimalException

from monster_light.domain.portfolio import InvalidMoney, Portfolio


class PortfolioNotFound(LookupError):
    """No persisted portfolio has the requested identifier."""


class SQLitePortfolioRepository:
    """Store authoritative snapshots; callers own connection lifetime.

    Saves commit when no transaction exists, or join the caller's transaction.
    Snapshot replacement does not provide stale-write detection.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS portfolios (
                portfolio_id TEXT PRIMARY KEY NOT NULL,
                cash TEXT NOT NULL CHECK (typeof(cash) = 'text')
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                portfolio_id TEXT NOT NULL REFERENCES portfolios(portfolio_id),
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL
                    CHECK (typeof(quantity) = 'integer' AND quantity > 0),
                PRIMARY KEY (portfolio_id, symbol)
            )
        """)

    def save(self, portfolio_id: str, portfolio: Portfolio) -> None:
        # A savepoint is atomic even with autocommit or an outer transaction.
        self.connection.execute("SAVEPOINT portfolio_snapshot")
        try:
            self.connection.execute("""
                INSERT INTO portfolios (portfolio_id, cash) VALUES (?, ?)
                ON CONFLICT (portfolio_id) DO UPDATE SET cash = excluded.cash
            """, (portfolio_id, str(portfolio.cash)))
            self.connection.execute(
                "DELETE FROM positions WHERE portfolio_id = ?", (portfolio_id,)
            )
            self.connection.executemany(
                "INSERT INTO positions (portfolio_id, symbol, quantity) VALUES (?, ?, ?)",
                ((portfolio_id, symbol, quantity)
                 for symbol, quantity in portfolio.positions.items()),
            )
            self.connection.execute("RELEASE SAVEPOINT portfolio_snapshot")
        except BaseException:
            self.connection.execute("ROLLBACK TO SAVEPOINT portfolio_snapshot")
            self.connection.execute("RELEASE SAVEPOINT portfolio_snapshot")
            raise

    def load(self, portfolio_id: str) -> Portfolio:
        rows = self.connection.execute("""
            SELECT p.cash, s.symbol, s.quantity
            FROM portfolios AS p
            LEFT JOIN positions AS s ON s.portfolio_id = p.portfolio_id
            WHERE p.portfolio_id = ?
        """, (portfolio_id,)).fetchall()
        if not rows:
            raise PortfolioNotFound(portfolio_id)
        try:
            cash = Decimal(rows[0][0])
        except (DecimalException, TypeError, ValueError) as error:
            raise InvalidMoney("Persisted cash must be decimal text") from error
        return Portfolio.restore(
            cash, {symbol: quantity for _, symbol, quantity in rows if symbol is not None}
        )
