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


@pytest.mark.parametrize('initial', list(ProposalStatus))
@pytest.mark.parametrize('result', list(ProposalStatus))
def test_sql_status_transition_matrix(database, initial, result):
    _, connection, repository = database
    proposal = candidate()
    repository.save(proposal)
    if initial is ProposalStatus.EXECUTED:
        repository.mark_approved(proposal.proposal_id)
    if initial is not ProposalStatus.PENDING:
        connection.execute(
            'UPDATE trade_proposals SET status = ?, rejection_reason = ?',
            (initial.value, 'No thanks' if initial is ProposalStatus.REJECTED else None),
        )
    parameters = (result.value, 'No thanks' if result is ProposalStatus.REJECTED else None)
    statement = 'UPDATE trade_proposals SET status = ?, rejection_reason = ?'
    if (initial, result) in {
        (ProposalStatus.PENDING, ProposalStatus.APPROVED),
        (ProposalStatus.PENDING, ProposalStatus.REJECTED),
        (ProposalStatus.APPROVED, ProposalStatus.EXECUTED),
    }:
        connection.execute(statement, parameters)
        assert repository.load(proposal.proposal_id).status is result
    else:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)
        assert repository.load(proposal.proposal_id).status is initial


@pytest.mark.parametrize('rollback', [False, True])
def test_legacy_schema_migration_preserves_history_and_transaction(tmp_path, rollback):
    from monster_light.application.approval import ApprovalService

    connection = sqlite3.connect(tmp_path / 'legacy.sqlite')
    try:
        # Recreate the pre-approval table and its original transition constraint.
        connection.execute("""
            CREATE TABLE trade_proposals (
                proposal_id TEXT PRIMARY KEY NOT NULL,
                created_at TEXT NOT NULL,
                origin TEXT NOT NULL CHECK (origin = 'MANUAL'),
                portfolio_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL
                    CHECK (typeof(quantity) = 'integer' AND quantity > 0),
                price TEXT NOT NULL CHECK (typeof(price) = 'text'),
                rationale TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'REJECTED')),
                rejection_reason TEXT,
                CHECK ((status = 'PENDING' AND rejection_reason IS NULL) OR
                       (status = 'REJECTED' AND rejection_reason IS NOT NULL
                        AND length(trim(rejection_reason)) > 0))
            )
        """)
        connection.execute("""
            CREATE TRIGGER trade_proposals_reject_only BEFORE UPDATE ON trade_proposals
            WHEN OLD.status != 'PENDING' OR NEW.status != 'REJECTED'
            BEGIN SELECT RAISE(ABORT, 'Only PENDING proposals may be rejected'); END
        """)
        repository = SQLiteProposalRepository(connection)
        pending, rejected = candidate(), candidate()
        repository.save(pending)
        repository.save(rejected)
        rejected = repository.reject(rejected.proposal_id, 'Not suitable')
        connection.execute('BEGIN')
        repository.initialize_schema()
        repository.initialize_schema()
        assert connection.in_transaction
        assert repository.load(pending.proposal_id) == pending
        assert repository.load(rejected.proposal_id) == rejected
        ApprovalService(repository).approve(pending.proposal_id, 'Human')
        if rollback:
            connection.rollback()
            assert repository.load(pending.proposal_id) == pending
            with pytest.raises(sqlite3.IntegrityError):
                repository.mark_approved(pending.proposal_id)
            connection.rollback()
            repository.initialize_schema()
            ApprovalService(repository).approve(pending.proposal_id, 'Human')
        else:
            connection.commit()
        assert repository.load(pending.proposal_id) == replace(pending, status=ProposalStatus.APPROVED)
        assert repository.load(rejected.proposal_id) == rejected
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE trade_proposals SET symbol = 'Changed'")
    finally:
        connection.close()


@pytest.mark.parametrize('rollback', [False, True])
@pytest.mark.parametrize('foreign_keys', [False, True])
@pytest.mark.parametrize('legacy_version', ['pre_execution', 'pre_ai'])
def test_legacy_migration_preserves_approvals(tmp_path, rollback, foreign_keys, legacy_version):
    from monster_light.application.ai_proposal import AIProposalService
    from monster_light.application.approval import ApprovalService
    from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository

    # Build the immediately preceding schema, including its named triggers.
    with sqlite3.connect(':memory:') as template:
        SQLiteProposalRepository(template).initialize_schema()
        table = template.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'trade_proposals'"
        ).fetchone()[0].replace("origin IN ('MANUAL', 'AI')", "origin = 'MANUAL'")
        if legacy_version == 'pre_execution':
            table = table.replace(", 'EXECUTED'", '')
        triggers = template.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'trade_proposals'"
        ).fetchall()
    connection = sqlite3.connect(tmp_path / 'approved-legacy.sqlite')
    try:
        connection.execute(f'PRAGMA foreign_keys = {int(foreign_keys)}')
        connection.execute(table)
        for (trigger,) in triggers:
            if legacy_version == 'pre_execution':
                trigger = trigger.replace(
                    "WHEN NOT ((OLD.status = 'PENDING' AND NEW.status IN ('APPROVED', 'REJECTED'))\n"
                    "                      OR (OLD.status = 'APPROVED' AND NEW.status = 'EXECUTED'))",
                    "WHEN OLD.status != 'PENDING' OR NEW.status NOT IN ('APPROVED', 'REJECTED')",
                )
            connection.execute(trigger)
        proposals = SQLiteProposalRepository(connection)
        pending, approved, rejected = candidate(), candidate(), candidate()
        for proposal in (pending, approved, rejected):
            proposals.save(proposal)
        approval = ApprovalService(proposals).approve(approved.proposal_id, 'Human')
        approved = proposals.load(approved.proposal_id)
        rejected = proposals.reject(rejected.proposal_id, 'No')
        approval_schema = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'human_approvals' ORDER BY name"
        ).fetchall()
        connection.execute('BEGIN')
        connection.execute('PRAGMA defer_foreign_keys = ON')
        proposals.initialize_schema()
        proposals.initialize_schema()
        assert connection.in_transaction
        assert connection.execute('PRAGMA defer_foreign_keys').fetchone()[0] == 1
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == int(foreign_keys)
        assert connection.execute('PRAGMA legacy_alter_table').fetchone()[0] == 0
        for proposal in (pending, approved, rejected):
            assert proposals.load(proposal.proposal_id) == proposal
            assert proposals.load(proposal.proposal_id).origin is ProposalOrigin.MANUAL
        assert SQLiteApprovalRepository(connection).load(approved.proposal_id) == approval
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'human_approvals' ORDER BY name"
        ).fetchall() == approval_schema
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        proposals.mark_executed(approved.proposal_id)
        ai_terms = dict(portfolio_id='one', side=TradeSide.BUY, symbol='AAPL',
                        quantity=1, price=Decimal('1.00'), rationale='AI suggestion')
        AIProposalService(proposals).create(**ai_terms)
        if rollback:
            connection.rollback()
            assert proposals.load(approved.proposal_id) == approved
            if legacy_version == 'pre_execution':
                with pytest.raises(sqlite3.IntegrityError):
                    proposals.mark_executed(approved.proposal_id)
            with pytest.raises(sqlite3.IntegrityError):
                AIProposalService(proposals).create(**ai_terms)
            proposals.initialize_schema()
            proposals.mark_executed(approved.proposal_id)
        else:
            connection.commit()
        assert proposals.load(approved.proposal_id).status is ProposalStatus.EXECUTED
        # The existing approval trigger still points at the real proposal table.
        ApprovalService(proposals).approve(pending.proposal_id, 'Another human')
        assert SQLiteApprovalRepository(connection).load(approved.proposal_id) == approval
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE trade_proposals SET symbol = 'changed'")
        connection.rollback()
        with sqlite3.connect(tmp_path / 'approved-legacy.sqlite') as reopened:
            assert SQLiteProposalRepository(reopened).load(pending.proposal_id).origin is ProposalOrigin.MANUAL
    finally:
        connection.close()


def test_mark_executed_guards_and_terminal_state(database):
    from monster_light.application.approval import ApprovalService
    from monster_light.application.proposal import ProposalAlreadyExecuted, ProposalNotApproved

    _, _, repository = database
    pending, rejected, approved = candidate(), candidate(), candidate()
    for proposal in (pending, rejected, approved):
        repository.save(proposal)
    repository.reject(rejected.proposal_id, 'No')
    for proposal in (pending, rejected):
        with pytest.raises(ProposalNotApproved):
            repository.mark_executed(proposal.proposal_id)
    with pytest.raises(ProposalNotFound):
        repository.mark_executed('unknown')
    ApprovalService(repository).approve(approved.proposal_id, 'Human')
    executed = repository.mark_executed(approved.proposal_id)
    assert executed == replace(approved, status=ProposalStatus.EXECUTED)
    for operation in (
        lambda: repository.mark_executed(approved.proposal_id),
        lambda: repository.mark_approved(approved.proposal_id),
        lambda: repository.reject(approved.proposal_id, 'No'),
        lambda: ApprovalService(repository).approve(approved.proposal_id, 'Human'),
    ):
        with pytest.raises(ProposalAlreadyExecuted):
            operation()
    assert repository.load(approved.proposal_id) == executed
