"""Proposal persistence and the boundary between candidates and execution."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from monster_light.application.proposal import (
    ProposalAlreadyRejected, ProposalNotFound, ProposalOrigin, ProposalStatus,
    TradeProposal,
)
from monster_light.application.trade_service import TradeService, TradeSide
from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


def candidate(**changes):
    fields = dict(portfolio_id="one", side=TradeSide.BUY, symbol=" aApL \t",
                  quantity=3, price=Decimal("123.4567890123456789012345678900"),
                  rationale="Consider adding shares")
    return TradeProposal(**(fields | changes))


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "proposals.sqlite"
    connection = sqlite3.connect(path)
    repository = SQLiteProposalRepository(connection)
    repository.initialize_schema()
    yield path, connection, repository
    connection.close()


def test_round_trip_and_reopen(database):
    path, connection, repository = database
    proposal = candidate()
    repository.save(proposal)
    assert repository.load(proposal.proposal_id) == proposal
    assert proposal.created_at.utcoffset() == timedelta(0)
    assert proposal.origin is ProposalOrigin.MANUAL
    assert proposal.status is ProposalStatus.PENDING
    assert connection.execute("SELECT price, typeof(price), symbol FROM trade_proposals").fetchone() == (
        str(proposal.price), "text", " aApL \t")
    assert not connection.in_transaction
    connection.close()
    with sqlite3.connect(path) as reopened:
        loaded = SQLiteProposalRepository(reopened).load(proposal.proposal_id)
        assert loaded == proposal
        assert loaded.price.as_tuple() == proposal.price.as_tuple()


def test_save_does_not_repeat_construction_validation(database, monkeypatch):
    _, _, repository = database
    proposal = candidate()

    def forbidden(*args, **kwargs):
        pytest.fail("Saving a proposal repeated construction validation")

    with monkeypatch.context() as patch:
        patch.setattr(TradeProposal, "__post_init__", forbidden)
        repository.save(proposal)
    assert repository.load(proposal.proposal_id) == proposal


def test_portfolio_isolation(database):
    _, _, repository = database
    first, second, third = candidate(), candidate(portfolio_id="two"), candidate()
    for proposal in (first, second, third):
        repository.save(proposal)
    assert repository.list_for_portfolio("one") == [first, third]
    assert repository.list_for_portfolio("two") == [second]
    assert repository.list_for_portfolio("missing") == []


def test_unknown_ids(database):
    _, _, repository = database
    with pytest.raises(ProposalNotFound):
        repository.load("missing")
    with pytest.raises(ProposalNotFound):
        repository.reject("missing", "Not suitable")


@pytest.mark.parametrize("field, value", [
    (field, value)
    for field in ("portfolio_id", "symbol", "rationale")
    for value in ("", " \t\n", None, 7)
] + [
    ("side", "BUY"), ("side", None),
    *[("quantity", value) for value in (0, -1, True, False, 1.5, "1", None)],
    *[("price", value) for value in (
        Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("sNaN"),
        Decimal("Infinity"), Decimal("-Infinity"), 1.5, 1, "1", None)],
    ("origin", "MANUAL"), ("origin", "AI"),
    ("status", "PENDING"), ("status", "APPROVED"), ("status", "EXECUTED"),
    ("created_at", datetime(2026, 1, 1)),
    ("created_at", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
    ("proposal_id", ""), ("rejection_reason", "Premature"),
    ("status", ProposalStatus.REJECTED),
])
def test_structural_validation(field, value):
    with pytest.raises(ValueError):
        candidate(**{field: value})


@pytest.mark.parametrize("side", list(TradeSide))
def test_creation_has_no_execution_effects(database, monkeypatch, side):
    _, connection, repository = database
    portfolios = SQLitePortfolioRepository(connection)
    portfolios.initialize_schema()
    portfolios.save("one", Portfolio(Decimal("0")))
    audits = SQLiteAuditRepository(connection)
    audits.initialize_schema()

    def forbidden(*args, **kwargs):
        pytest.fail("Proposal creation crossed the execution boundary")

    with monkeypatch.context() as patch:
        for cls, methods in (
            (SQLitePortfolioRepository, ("load", "save")),
            (Portfolio, ("buy", "sell", "quantity_for")),
            (TradeService, ("execute",)), (SQLiteAuditRepository, ("append",)),
        ):
            for method in methods:
                patch.setattr(cls, method, forbidden)
        repository.save(candidate(side=side))
        repository.save(candidate(side=side, portfolio_id="nonexistent"))
    portfolio = portfolios.load("one")
    assert portfolio.cash == Decimal("0")
    assert portfolio.positions == {}
    assert connection.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 0


def test_rejection_preserves_terms_and_survives_reopen(database):
    path, connection, repository = database
    proposal = candidate()
    repository.save(proposal)
    rejected = repository.reject(proposal.proposal_id, " Too concentrated ")
    expected = replace(proposal, status=ProposalStatus.REJECTED,
                       rejection_reason=" Too concentrated ")
    assert rejected == expected
    assert rejected.price.as_tuple() == proposal.price.as_tuple()
    with pytest.raises(ProposalAlreadyRejected):
        repository.reject(proposal.proposal_id, "Different reason")
    assert repository.load(proposal.proposal_id) == expected
    connection.close()
    with sqlite3.connect(path) as reopened:
        assert SQLiteProposalRepository(reopened).load(proposal.proposal_id) == expected


@pytest.mark.parametrize("reason", ["", " \t", None, 7])
def test_invalid_rejection_reason_leaves_pending(database, reason):
    _, _, repository = database
    proposal = candidate()
    repository.save(proposal)
    with pytest.raises(ValueError):
        repository.reject(proposal.proposal_id, reason)
    assert repository.load(proposal.proposal_id) == proposal


def test_save_cannot_rewrite_history(database):
    _, _, repository = database
    proposal = candidate()
    repository.save(proposal)
    with pytest.raises(FrozenInstanceError):
        proposal.symbol = "MSFT"
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(replace(proposal, symbol="MSFT"))
    assert repository.load(proposal.proposal_id) == proposal
    with pytest.raises(ValueError, match="PENDING"):
        repository.save(replace(candidate(), status=ProposalStatus.REJECTED,
                                rejection_reason="Already rejected"))


@pytest.mark.parametrize("statement", [
    "UPDATE trade_proposals SET symbol = 'MSFT'",
    "UPDATE trade_proposals SET price = '1'",
    "UPDATE trade_proposals SET rationale = 'Changed'",
    "DELETE FROM trade_proposals",
    "INSERT OR REPLACE INTO trade_proposals SELECT * FROM trade_proposals",
])
def test_sql_cannot_rewrite_history(database, statement):
    _, connection, repository = database
    proposal = candidate()
    repository.save(proposal)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
    assert repository.load(proposal.proposal_id) == proposal


@pytest.mark.parametrize("commit", [False, True])
def test_outer_transaction_ownership(database, commit):
    _, connection, repository = database
    connection.execute("BEGIN")
    proposal = candidate()
    repository.save(proposal)
    repository.reject(proposal.proposal_id, "No thanks")
    assert connection.in_transaction
    if commit:
        connection.commit()
        assert repository.load(proposal.proposal_id).status is ProposalStatus.REJECTED
    else:
        connection.rollback()
        with pytest.raises(ProposalNotFound):
            repository.load(proposal.proposal_id)
