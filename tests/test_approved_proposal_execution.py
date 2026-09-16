"""Approval gates execution; authoritative validation and atomicity still apply."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from monster_light.application.approval import ApprovalService
from monster_light.application.proposal import (
    ProposalAlreadyExecuted, ProposalNotApproved, ProposalNotFound, ProposalStatus, TradeProposal,
)
from monster_light.application.proposal_execution import (
    ApprovedProposalExecutionService, HumanApprovalRequired,
)
from monster_light.application.trade_service import TradeRequest, TradeService, TradeSide
from monster_light.domain.portfolio import InsufficientCash, InsufficientShares, Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'execution.sqlite'
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    proposals = SQLiteProposalRepository(connection)
    proposals.initialize_schema()
    SQLiteApprovalRepository(connection).initialize_schema()
    SQLiteAuditRepository(connection).initialize_schema()
    portfolios = SQLitePortfolioRepository(connection)
    portfolios.initialize_schema()
    portfolios.save(' one ', Portfolio.restore(Decimal('80.000'), {'AAPL': 2}))
    yield path, connection, proposals, portfolios, ApprovedProposalExecutionService(proposals)
    connection.close()


def candidate(proposals, **changes):
    proposal = TradeProposal(**(dict(
        portfolio_id=' one ', side=TradeSide.BUY, symbol=' aApL \t',
        quantity=1, price=Decimal('10.2500'), rationale='Sell everything! This is not authority.',
    ) | changes))
    proposals.save(proposal)
    return proposal


def audits(connection):
    return connection.execute('SELECT * FROM trade_audits').fetchall()


def assert_original(portfolios):
    portfolio = portfolios.load(' one ')
    assert portfolio.cash == Decimal('80')
    assert portfolio.positions == {'AAPL': 2}


@pytest.mark.parametrize('side,cash,shares', [
    (TradeSide.BUY, '69.7500', 3), (TradeSide.SELL, '90.2500', 1),
])
def test_success_and_reopen(database, monkeypatch, side, cash, shares):
    path, connection, proposals, portfolios, service = database
    proposal = candidate(proposals, side=side)
    approval = ApprovalService(proposals).approve(proposal.proposal_id, 'Human')
    execute = TradeService.execute
    seen = []

    def capture(self, request, **kwargs):
        seen.append(request)
        return execute(self, request, **kwargs)

    monkeypatch.setattr(TradeService, 'execute', capture)
    result = service.execute(proposal.proposal_id)
    assert seen == [TradeRequest(proposal.portfolio_id, side, proposal.symbol,
                                 proposal.quantity, proposal.price)]
    assert seen[0].price.as_tuple() == proposal.price.as_tuple()
    assert result.cash == Decimal(cash)
    assert result.positions == {'AAPL': shares}
    assert proposals.load(proposal.proposal_id) == replace(proposal, status=ProposalStatus.EXECUTED)
    row, = audits(connection)
    assert (row['outcome'], row['reason_code'], row['origin']) == ('ACCEPTED', 'TradeExecuted', 'APPROVED_PROPOSAL')
    assert (row['proposal_id'], row['approval_id']) == (proposal.proposal_id, approval.approval_id)
    assert (row['portfolio_id'], row['side'], row['symbol'], row['quantity'], row['price']) == (
        proposal.portfolio_id, side.value, proposal.symbol, '1', '10.2500')
    assert SQLiteApprovalRepository(connection).load(proposal.proposal_id) == approval
    assert connection.execute('SELECT count(*) FROM human_approvals').fetchone()[0] == 1
    assert not connection.in_transaction
    before = tuple(row)
    with pytest.raises(ProposalAlreadyExecuted):
        service.execute(proposal.proposal_id)
    assert [tuple(row) for row in audits(connection)] == [before]
    connection.close()
    with sqlite3.connect(path) as reopened:
        restored = SQLiteProposalRepository(reopened)
        restored.initialize_schema()
        assert restored.load(proposal.proposal_id).status is ProposalStatus.EXECUTED
        assert SQLiteApprovalRepository(reopened).load(proposal.proposal_id) == approval
        portfolio = SQLitePortfolioRepository(reopened).load(' one ')
        assert portfolio.cash == Decimal(cash)
        assert portfolio.positions == {'AAPL': shares}
        assert reopened.execute('SELECT origin, proposal_id, approval_id FROM trade_audits').fetchone() == (
            'APPROVED_PROPOSAL', proposal.proposal_id, approval.approval_id)


@pytest.mark.parametrize('status', [ProposalStatus.PENDING, ProposalStatus.REJECTED])
def test_unapproved_cannot_reach_trade_service(database, monkeypatch, status):
    _, connection, proposals, portfolios, service = database
    proposal = candidate(proposals)
    if status is ProposalStatus.REJECTED:
        proposals.reject(proposal.proposal_id, 'No')

    def forbidden(*args):
        pytest.fail('Unapproved proposal reached TradeService')

    monkeypatch.setattr(TradeService, 'execute', forbidden)
    with pytest.raises(ProposalNotApproved):
        service.execute(proposal.proposal_id)
    assert proposals.load(proposal.proposal_id).status is status
    assert audits(connection) == []
    assert_original(portfolios)


def test_missing_approval_and_unknown_id(database, monkeypatch):
    _, connection, proposals, portfolios, service = database
    proposal = candidate(proposals)
    proposals.mark_approved(proposal.proposal_id)

    def forbidden(*args):
        pytest.fail('Missing approval reached TradeService')

    monkeypatch.setattr(TradeService, 'execute', forbidden)
    with pytest.raises(HumanApprovalRequired):
        service.execute(proposal.proposal_id)
    with pytest.raises(ProposalNotFound):
        service.execute('missing')
    assert proposals.load(proposal.proposal_id).status is ProposalStatus.APPROVED
    assert audits(connection) == []
    assert_original(portfolios)


@pytest.mark.parametrize('side,quantity,error', [
    (TradeSide.SELL, 2, InsufficientShares),
    (TradeSide.BUY, 100, InsufficientCash),
])
def test_rejection_keeps_decision_and_provenance(database, monkeypatch, side, quantity, error):
    path, connection, proposals, portfolios, service = database
    proposal = candidate(proposals, side=side, quantity=quantity)
    approval = ApprovalService(proposals).approve(proposal.proposal_id, 'Human')
    # The authoritative balance changes after the human decision.
    TradeService(portfolios).execute(TradeRequest(' one ', TradeSide.SELL, 'AAPL', 1, Decimal('1')))
    before = portfolios.load(' one ')
    errors = []
    original = TradeService.execute

    def capture(self, request, **kwargs):
        try:
            return original(self, request, **kwargs)
        except error as caught_error:
            errors.append(caught_error)
            raise

    monkeypatch.setattr(TradeService, 'execute', capture)
    with pytest.raises(error) as caught:
        service.execute(proposal.proposal_id)
    assert caught.value is errors[0]
    assert proposals.load(proposal.proposal_id).status is ProposalStatus.APPROVED
    assert SQLiteApprovalRepository(connection).load(proposal.proposal_id) == approval
    after = portfolios.load(' one ')
    assert (after.cash, after.positions) == (before.cash, before.positions)
    row = audits(connection)[-1]
    assert (row['outcome'], row['reason_code'], row['reason_message']) == (
        'REJECTED', error.__name__, str(caught.value))
    assert (row['origin'], row['proposal_id'], row['approval_id']) == (
        'APPROVED_PROPOSAL', proposal.proposal_id, approval.approval_id)
    assert connection.execute('SELECT count(*) FROM human_approvals').fetchone()[0] == 1
    assert not connection.in_transaction
    with sqlite3.connect(path) as observer:
        assert observer.execute(
            "SELECT origin, proposal_id, approval_id FROM trade_audits WHERE outcome = 'REJECTED'"
        ).fetchone() == ('APPROVED_PROPOSAL', proposal.proposal_id, approval.approval_id)


@pytest.mark.parametrize('after', [False, True])
@pytest.mark.parametrize('outer', [False, True])
def test_mark_failure_rolls_back_trade_and_audit(database, monkeypatch, after, outer):
    _, connection, proposals, portfolios, service = database
    proposal = candidate(proposals)
    approval = ApprovalService(proposals).approve(proposal.proposal_id, 'Human')
    if outer:
        connection.execute('BEGIN')
        portfolios.save('caller', Portfolio(Decimal('7')))
    mark = proposals.mark_executed

    def fail(proposal_id):
        if after:
            mark(proposal_id)
        raise sqlite3.OperationalError('mark failed')

    monkeypatch.setattr(proposals, 'mark_executed', fail)
    with pytest.raises(sqlite3.OperationalError, match='mark failed'):
        service.execute(proposal.proposal_id)
    assert connection.in_transaction is outer
    assert_original(portfolios)
    assert proposals.load(proposal.proposal_id).status is ProposalStatus.APPROVED
    assert SQLiteApprovalRepository(connection).load(proposal.proposal_id) == approval
    assert audits(connection) == []
    if outer:
        assert portfolios.load('caller').cash == Decimal('7')
        connection.commit()


@pytest.mark.parametrize('commit', [False, True])
@pytest.mark.parametrize('rejected', [False, True])
def test_outer_transaction_is_caller_owned(database, commit, rejected):
    path, connection, proposals, portfolios, service = database
    proposal = candidate(proposals, side=TradeSide.SELL, quantity=3 if rejected else 1)
    ApprovalService(proposals).approve(proposal.proposal_id, 'Human')
    connection.execute('BEGIN')
    if rejected:
        with pytest.raises(InsufficientShares):
            service.execute(proposal.proposal_id)
    else:
        service.execute(proposal.proposal_id)
    assert connection.in_transaction
    assert len(audits(connection)) == 1
    with sqlite3.connect(path) as observer:
        assert audits(observer) == []
        assert SQLiteProposalRepository(observer).load(proposal.proposal_id).status is ProposalStatus.APPROVED
        assert_original(SQLitePortfolioRepository(observer))
    if commit:
        connection.commit()
    else:
        connection.rollback()
    assert len(audits(connection)) == int(commit)
    assert proposals.load(proposal.proposal_id).status is (
        ProposalStatus.EXECUTED if commit and not rejected else ProposalStatus.APPROVED)
    if commit and not rejected:
        assert portfolios.load(' one ').positions == {'AAPL': 1}
    else:
        assert_original(portfolios)
