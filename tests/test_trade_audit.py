"""Durable evidence and transaction invariants for direct trades."""

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from monster_light.application.audit import AuditOrigin, ExecutionAuditContext
from monster_light.application.trade_service import TradeRequest, TradeService, TradeSide
from monster_light.domain.portfolio import (
    InsufficientCash, InsufficientShares, InvalidMoney, InvalidQuantity,
    InvalidSymbol, Portfolio, PositionNotFound,
)
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_repository import PortfolioNotFound, SQLitePortfolioRepository


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "audit.sqlite"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    repository = SQLitePortfolioRepository(connection)
    repository.initialize_schema()
    repository.save("one", Portfolio.restore(Decimal("80.000"), {"AAPL": 2}))
    SQLiteAuditRepository(connection).initialize_schema()
    yield path, connection, repository
    connection.close()


def records(connection):
    return connection.execute("SELECT * FROM trade_audits").fetchall()


@pytest.mark.parametrize("side, quantity, cash, owned", [
    (TradeSide.BUY, 3, "49.250", 5),
    (TradeSide.SELL, 1, "90.250", 1),
    (TradeSide.SELL, 2, "100.500", 0),
])
def test_accepted_evidence_survives_reopening(database, side, quantity, cash, owned):
    path, connection, repository = database
    TradeService(repository).execute(TradeRequest("one", side, " aapl ", quantity, Decimal("10.250")))
    assert not connection.in_transaction
    connection.close()
    with sqlite3.connect(path) as reopened:
        reopened.row_factory = sqlite3.Row
        rows = records(reopened)
        assert len(rows) == 1
        row = rows[0]
        assert UUID(row["audit_id"])
        assert datetime.fromisoformat(row["timestamp"]).utcoffset() == timedelta(0)
        assert (row["origin"], row["outcome"], row["reason_code"]) == ("DIRECT", "ACCEPTED", "TradeExecuted")
        assert row["proposal_id"] is None
        assert row["approval_id"] is None
        assert row["market_evidence_id"] is None
        assert row["reason_message"]
        assert (row["portfolio_id"], row["side"], row["symbol"]) == ("one", side.value, " aapl ")
        assert (row["quantity"], row["price"]) == (str(quantity), "10.250")
        assert (row["cash_before"], row["cash_after"]) == ("80.000", cash)
        assert (row["quantity_before"], row["quantity_after"]) == (2, owned)
        assert tuple(reopened.execute("SELECT typeof(price), typeof(cash_before), typeof(cash_after) FROM trade_audits").fetchone()) == ("text", "text", "text")
        assert str(SQLitePortfolioRepository(reopened).load("one").cash) == cash


@pytest.mark.parametrize("side, symbol, quantity, price, error, owned", [
    (TradeSide.SELL, "aapl", 3, Decimal("10"), InsufficientShares, 2),
    (TradeSide.BUY, "AAPL", 9, Decimal("10"), InsufficientCash, 2),
    (TradeSide.SELL, "MSFT", 1, Decimal("10"), PositionNotFound, 0),
    (TradeSide.BUY, " ", 1, Decimal("10"), InvalidSymbol, None),
    (TradeSide.BUY, [], 1, Decimal("10"), InvalidSymbol, None),
    (TradeSide.BUY, "AAPL", True, Decimal("10"), InvalidQuantity, 2),
    (TradeSide.SELL, "AAPL", 0, Decimal("10"), InvalidQuantity, 2),
    (TradeSide.BUY, "AAPL", 1, Decimal("NaN"), InvalidMoney, 2),
    (TradeSide.SELL, "AAPL", 1, Decimal("-1"), InvalidMoney, 2),
    (TradeSide.BUY, "AAPL", 1, 1.5, InvalidMoney, 2),
])
def test_rejected_domain_requests(database, side, symbol, quantity, price, error, owned):
    path, connection, repository = database
    with pytest.raises(error) as caught:
        TradeService(repository).execute(TradeRequest("one", side, symbol, quantity, price))
    assert not connection.in_transaction
    row, = records(connection)
    assert (row["outcome"], row["reason_code"], row["reason_message"]) == ("REJECTED", error.__name__, str(caught.value))
    assert row["cash_before"] == row["cash_after"] == "80.000"
    assert row["quantity_before"] == row["quantity_after"] == owned
    assert row["price"] == str(price)
    with sqlite3.connect(path) as reopened:
        assert reopened.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 1
        snapshot = SQLitePortfolioRepository(reopened).load("one")
        assert snapshot.cash == Decimal("80")
        assert snapshot.positions == {"AAPL": 2}


def test_unknown_portfolio(database):
    _, connection, repository = database
    with pytest.raises(PortfolioNotFound):
        TradeService(repository).execute(TradeRequest("missing", TradeSide.BUY, "AAPL", 1, Decimal("1")))
    row, = records(connection)
    assert (row["outcome"], row["reason_code"]) == ("REJECTED", "PortfolioNotFound")
    assert all(row[field] is None for field in ("cash_before", "cash_after", "quantity_before", "quantity_after"))


@pytest.mark.parametrize("portfolio_id, side", [("", TradeSide.BUY), (None, TradeSide.SELL), ("one", "BUY")])
def test_structural_failures_are_unaudited(database, portfolio_id, side):
    _, connection, repository = database
    with pytest.raises(ValueError):
        TradeService(repository).execute(TradeRequest(portfolio_id, side, "AAPL", 1, Decimal("1")))
    assert records(connection) == []


@pytest.mark.parametrize("outer", [False, True])
def test_failed_audit_rolls_back_only_trade(database, outer, monkeypatch):
    _, connection, repository = database
    if outer:
        connection.execute("BEGIN")
        repository.save("other", Portfolio(Decimal("12")))
    original = SQLiteAuditRepository.append

    def fail_after_insert(self, audit):
        original(self, audit)
        raise sqlite3.OperationalError("audit failed")

    monkeypatch.setattr(SQLiteAuditRepository, "append", fail_after_insert)
    with pytest.raises(sqlite3.OperationalError, match="audit failed"):
        TradeService(repository).execute(TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("1")))
    assert connection.in_transaction is outer
    assert repository.load("one").cash == Decimal("80")
    assert repository.load("one").positions == {"AAPL": 2}
    assert records(connection) == []
    if outer:
        assert repository.load("other").cash == Decimal("12")
        connection.commit()


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_outer_transaction_retains_ownership(database, commit, rejected):
    path, connection, repository = database
    connection.execute("BEGIN")
    request = TradeRequest("one", TradeSide.BUY, "AAPL", 100 if rejected else 1, Decimal("1"))
    if rejected:
        with pytest.raises(InsufficientCash):
            TradeService(repository).execute(request)
    else:
        TradeService(repository).execute(request)
    assert connection.in_transaction
    assert len(records(connection)) == 1
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 0
    if commit:
        connection.commit()
    else:
        connection.rollback()
    assert len(records(connection)) == int(commit)
    assert repository.load("one").cash == Decimal("79" if commit and not rejected else "80")


@pytest.mark.parametrize("statement", [
    "UPDATE trade_audits SET reason_message = 'changed'",
    "DELETE FROM trade_audits",
    "INSERT OR REPLACE INTO trade_audits SELECT * FROM trade_audits",
    "REPLACE INTO trade_audits SELECT * FROM trade_audits",
    "INSERT INTO trade_audits SELECT * FROM trade_audits",
])
@pytest.mark.parametrize("context", [ExecutionAuditContext(), ExecutionAuditContext(
    AuditOrigin.APPROVED_PROPOSAL, "proposal", "approval", "test-evidence")])
def test_evidence_cannot_be_rewritten(database, statement, context):
    _, connection, repository = database
    request = TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("0.1234567890123456789012345"))
    TradeService(repository).execute(request, audit_context=context)
    before = [tuple(row) for row in records(connection)]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(statement)
    assert [tuple(row) for row in records(connection)] == before
    connection.rollback()
    TradeService(repository).execute(request)
    rows = records(connection)
    assert len(rows) == 2
    assert rows[0]["audit_id"] != rows[1]["audit_id"]
    assert rows[0]["price"] == "0.1234567890123456789012345"


def test_original_domain_exception_is_preserved(database, monkeypatch):
    _, connection, repository = database
    original = InvalidMoney("original domain failure")

    def reject(*args):
        raise original

    monkeypatch.setattr(Portfolio, "buy", reject)
    with pytest.raises(InvalidMoney) as caught:
        TradeService(repository).execute(TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("1")))
    assert caught.value is original
    assert records(connection)[0]["reason_message"] == str(original)


def test_rejected_audit_failure_preserves_domain_error(database, monkeypatch):
    _, connection, repository = database

    def fail(*args):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(SQLiteAuditRepository, "append", fail)
    with pytest.raises(InsufficientCash) as caught:
        TradeService(repository).execute(TradeRequest("one", TradeSide.BUY, "AAPL", 100, Decimal("1")))
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert records(connection) == []
    assert repository.load("one").cash == Decimal("80")
    assert not connection.in_transaction


def test_autocommit_connection(database):
    path, connection, _ = database
    connection.close()
    with sqlite3.connect(path, isolation_level=None) as connection:
        repository = SQLitePortfolioRepository(connection)
        TradeService(repository).execute(TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("1")))
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 1
        assert repository.load("one").cash == Decimal("79")


@pytest.mark.parametrize("origin,proposal_id,approval_id", [
    ("DIRECT", "proposal", None), ("DIRECT", None, "approval"),
    ("DIRECT", "proposal", "approval"),
    ("APPROVED_PROPOSAL", None, None), ("APPROVED_PROPOSAL", "proposal", None),
    ("APPROVED_PROPOSAL", None, "approval"),
    ("APPROVED_PROPOSAL", "", "approval"), ("APPROVED_PROPOSAL", "proposal", " "),
    ("UNKNOWN", None, None),
])
def test_sqlite_rejects_invalid_provenance(database, origin, proposal_id, approval_id):
    _, connection, repository = database
    TradeService(repository).execute(TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("1")))
    row = dict(records(connection)[0])
    row.update(audit_id="invalid", origin=origin, proposal_id=proposal_id, approval_id=approval_id)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO trade_audits VALUES (" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
    assert len(records(connection)) == 1


@pytest.mark.parametrize("outer", [False, True])
def test_legacy_direct_migration_preserves_evidence_and_protections(tmp_path, outer):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS trade_audits (
                audit_id TEXT PRIMARY KEY NOT NULL,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL CHECK (origin = 'DIRECT'),
                portfolio_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                symbol TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price TEXT NOT NULL CHECK (typeof(price) = 'text'),
                outcome TEXT NOT NULL CHECK (outcome IN ('ACCEPTED', 'REJECTED')),
                reason_code TEXT NOT NULL,
                reason_message TEXT NOT NULL,
                cash_before TEXT CHECK (cash_before IS NULL OR typeof(cash_before) = 'text'),
                cash_after TEXT CHECK (cash_after IS NULL OR typeof(cash_after) = 'text'),
                quantity_before INTEGER,
                quantity_after INTEGER
            )
        """)
        old = ("old-id", "2026-01-01T00:00:00+00:00", "DIRECT", "one", "BUY",
               "AAPL", "1", "1.000", "ACCEPTED", "TradeExecuted", "Trade executed",
               "2.000", "1.000", 0, 1)
        connection.execute("INSERT INTO trade_audits VALUES (" + ",".join("?" for _ in old) + ")", old)
        repository = SQLiteAuditRepository(connection)
        repository._create_triggers()
        connection.commit()
        if outer:
            connection.execute("BEGIN")
        repository.initialize_schema()
        assert connection.in_transaction is outer
        assert connection.execute("SELECT * FROM trade_audits").fetchone() == old + (None, None, None)
        if outer:
            connection.rollback()
            assert connection.execute("SELECT * FROM trade_audits").fetchone() == old
            repository.initialize_schema()
        repository.initialize_schema()
        for statement in (
            "UPDATE trade_audits SET proposal_id = 'changed'",
            "DELETE FROM trade_audits",
            "INSERT OR REPLACE INTO trade_audits SELECT * FROM trade_audits",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
        connection.rollback()
    with sqlite3.connect(path) as reopened:
        assert reopened.execute("SELECT * FROM trade_audits").fetchone() == old + (None, None, None)
        portfolios = SQLitePortfolioRepository(reopened)
        portfolios.initialize_schema()
        portfolios.save("one", Portfolio(Decimal("10")))
        TradeService(portfolios).execute(
            TradeRequest("one", TradeSide.BUY, "AAPL", 1, Decimal("1")),
            audit_context=ExecutionAuditContext(AuditOrigin.APPROVED_PROPOSAL, "proposal", "approval", "test-evidence"),
        )
        assert reopened.execute("SELECT origin, proposal_id, approval_id, market_evidence_id FROM trade_audits WHERE audit_id != 'old-id'").fetchone() == (
            "APPROVED_PROPOSAL", "proposal", "approval", "test-evidence")


@pytest.mark.parametrize('origin,proposal,approval,evidence', [
    (AuditOrigin.DIRECT, None, None, 'evidence'),
    (AuditOrigin.DIRECT, 'proposal', None, None),
    (AuditOrigin.DIRECT, None, 'approval', None),
    (AuditOrigin.APPROVED_PROPOSAL, 'proposal', 'approval', None),
    (AuditOrigin.APPROVED_PROPOSAL, 'proposal', 'approval', ''),
    (AuditOrigin.APPROVED_PROPOSAL, 'proposal', 'approval', ' \t'),
    (AuditOrigin.APPROVED_PROPOSAL, None, 'approval', 'evidence'),
    (AuditOrigin.APPROVED_PROPOSAL, 'proposal', None, 'evidence'),
])
def test_new_context_requires_honest_complete_provenance(origin, proposal, approval, evidence):
    with pytest.raises(ValueError):
        ExecutionAuditContext(origin, proposal, approval, evidence)


@pytest.mark.parametrize('outer', [False, True])
def test_historical_proposal_audit_migration(tmp_path, outer):
    path = tmp_path / 'historical.sqlite'
    with sqlite3.connect(path) as connection:
        # Recreate the immediately preceding schema, including its constraints.
        connection.execute('''CREATE TABLE trade_audits (
            audit_id TEXT PRIMARY KEY NOT NULL, timestamp TEXT NOT NULL,
            origin TEXT NOT NULL CHECK (origin IN ('DIRECT', 'APPROVED_PROPOSAL')),
            portfolio_id TEXT NOT NULL, side TEXT NOT NULL, symbol TEXT NOT NULL,
            quantity TEXT NOT NULL, price TEXT NOT NULL, outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL, reason_message TEXT NOT NULL,
            cash_before TEXT, cash_after TEXT, quantity_before INTEGER, quantity_after INTEGER,
            proposal_id TEXT, approval_id TEXT,
            CHECK ((origin = 'DIRECT' AND proposal_id IS NULL AND approval_id IS NULL)
                OR (origin = 'APPROVED_PROPOSAL' AND proposal_id IS NOT NULL AND approval_id IS NOT NULL))
        )''')
        old = ('historical', '2026-01-01T00:00:00+00:00', 'APPROVED_PROPOSAL',
               'one', 'SELL', 'AAPL', '500', '1.000', 'REJECTED', 'InsufficientShares',
               'Not enough shares', '2.000', '2.000', 2, 2, 'original-proposal', 'original-approval')
        direct = ('direct',) + old[1:2] + ('DIRECT',) + old[3:15] + (None, None)
        connection.executemany('INSERT INTO trade_audits VALUES (' + ','.join('?' for _ in old) + ')',
                               [old, direct])
        repository = SQLiteAuditRepository(connection)
        repository._create_triggers()
        connection.commit()
        if outer:
            connection.execute('BEGIN')
        repository.initialize_schema()
        assert connection.in_transaction is outer
        assert connection.execute('SELECT * FROM trade_audits ORDER BY rowid').fetchall() == [old + (None,), direct + (None,)]
        if outer:
            connection.rollback()
            assert connection.execute('SELECT * FROM trade_audits ORDER BY rowid').fetchall() == [old, direct]
            repository.initialize_schema()
        repository.initialize_schema()
        for statement in ("UPDATE trade_audits SET market_evidence_id = 'invented'",
                          'DELETE FROM trade_audits',
                          'INSERT OR REPLACE INTO trade_audits SELECT * FROM trade_audits'):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                connection.execute(statement)
        connection.rollback()
    with sqlite3.connect(path) as reopened:
        assert reopened.execute('SELECT * FROM trade_audits ORDER BY rowid').fetchall() == [old + (None,), direct + (None,)]
