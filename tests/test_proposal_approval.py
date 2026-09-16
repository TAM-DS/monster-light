"""Human approval is durable evidence, never trade execution."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.proposal import (
    ProposalAlreadyApproved, ProposalAlreadyRejected, ProposalNotFound, ProposalStatus,
    TradeProposal,
)
from monster_light.application.trade_service import TradeService, TradeSide
from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'approvals.sqlite'
    connection = sqlite3.connect(path)
    proposals = SQLiteProposalRepository(connection)
    proposals.initialize_schema()
    approvals = SQLiteApprovalRepository(connection)
    approvals.initialize_schema()
    proposal = TradeProposal(portfolio_id='one', side=TradeSide.BUY, symbol=' aApL ',
                             quantity=3, price=Decimal('123.456789012345678900'),
                             rationale='Consider shares')
    proposals.save(proposal)
    yield path, connection, proposals, approvals, proposal, ApprovalService(proposals)
    connection.close()


def test_approve_preserves_terms_and_survives_reopen(database):
    path, connection, proposals, approvals, proposal, service = database
    before = datetime.now(timezone.utc)
    approval = service.approve(proposal.proposal_id, ' Human operator ')
    assert approval.proposal_id == proposal.proposal_id
    assert approval.approver == ' Human operator '
    assert before <= approval.approved_at <= datetime.now(timezone.utc)
    assert approval.approved_at.utcoffset() == timedelta(0)
    assert UUID(approval.approval_id).version == 4
    assert approvals.load(proposal.proposal_id) == approval
    approved = replace(proposal, status=ProposalStatus.APPROVED)
    assert proposals.load(proposal.proposal_id) == approved
    assert proposals.load(proposal.proposal_id).price.as_tuple() == proposal.price.as_tuple()
    other = replace(proposal, proposal_id='another')
    proposals.save(other)
    assert service.approve(other.proposal_id, 'Human').approval_id != approval.approval_id
    assert not connection.in_transaction
    connection.close()
    with sqlite3.connect(path) as reopened:
        repository = SQLiteProposalRepository(reopened)
        repository.initialize_schema()
        assert repository.load(proposal.proposal_id) == approved
        assert SQLiteApprovalRepository(reopened).load(proposal.proposal_id) == approval


def test_terminal_states_and_unknown_proposals(database):
    _, connection, proposals, approvals, proposal, service = database
    approval = service.approve(proposal.proposal_id, 'Human')
    with pytest.raises(ProposalAlreadyApproved):
        service.approve(proposal.proposal_id, 'Other human')
    with pytest.raises(ProposalAlreadyApproved):
        proposals.reject(proposal.proposal_id, 'Changed mind')
    rejected = replace(proposal, proposal_id='rejected')
    proposals.save(rejected)
    proposals.reject(rejected.proposal_id, 'No thanks')
    with pytest.raises(ProposalAlreadyRejected):
        service.approve(rejected.proposal_id, 'Human')
    with pytest.raises(ProposalNotFound):
        service.approve('missing', 'Human')
    assert approvals.load(proposal.proposal_id) == approval
    assert approvals.load(rejected.proposal_id) is None
    assert connection.execute('SELECT count(*) FROM human_approvals').fetchone()[0] == 1


@pytest.mark.parametrize('approver', ['', ' \n\t', None, 4, True, []])
def test_invalid_approver_changes_nothing(database, monkeypatch, approver):
    _, connection, proposals, approvals, proposal, service = database
    def forbidden(*args):
        pytest.fail('Invalid approver must fail before loading or persisting')
    with monkeypatch.context() as patch:
        patch.setattr(proposals, 'load', forbidden)
        with pytest.raises(ValueError, match='approver'):
            service.approve(proposal.proposal_id, approver)
    assert proposals.load(proposal.proposal_id) == proposal
    assert approvals.load(proposal.proposal_id) is None
    assert not connection.in_transaction


@pytest.mark.parametrize('field,value', [
    ('approval_id', ''), ('proposal_id', ''), ('approver', '\t'),
    ('approved_at', datetime(2026, 1, 1)),
    ('approved_at', datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
])
def test_approval_structure(field, value):
    with pytest.raises(ValueError):
        HumanApproval(**({'proposal_id': 'one', 'approver': 'Human'} | {field: value}))


@pytest.mark.parametrize('field', ['approval_id', 'proposal_id', 'approver', 'approved_at'])
def test_record_is_frozen(field):
    approval = HumanApproval(proposal_id='one', approver='Human')
    with pytest.raises(FrozenInstanceError):
        setattr(approval, field, 'changed')


@pytest.mark.parametrize('side', list(TradeSide))
def test_no_portfolio_or_execution_access(database, monkeypatch, side):
    _, connection, proposals, _, proposal, service = database
    portfolios = SQLitePortfolioRepository(connection)
    portfolios.initialize_schema()
    portfolios.save('one', Portfolio(Decimal('0')))
    audits = SQLiteAuditRepository(connection)
    audits.initialize_schema()
    def forbidden(*args, **kwargs):
        pytest.fail('Approval crossed the execution boundary')
    with monkeypatch.context() as patch:
        for cls, methods in (
            (SQLitePortfolioRepository, ('load', 'save')),
            (Portfolio, ('buy', 'sell', 'quantity_for')),
            (TradeService, ('execute',)), (SQLiteAuditRepository, ('append',)),
        ):
            for method in methods:
                patch.setattr(cls, method, forbidden)
        for portfolio_id in ('one', 'nonexistent'):
            candidate = replace(proposal, proposal_id=f'{side.value}-{portfolio_id}',
                                portfolio_id=portfolio_id, side=side)
            proposals.save(candidate)
            service.approve(candidate.proposal_id, 'Human')
    portfolio = portfolios.load('one')
    assert portfolio.cash == Decimal('0')
    assert portfolio.positions == {}
    assert connection.execute('SELECT count(*) FROM trade_audits').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['append_before', 'append_after', 'mark_before', 'mark_after'])
def test_write_failure_rolls_back_both_records(database, monkeypatch, failure):
    _, connection, proposals, approvals, proposal, service = database
    cls, method = ((SQLiteApprovalRepository, 'append') if failure.startswith('append')
                   else (SQLiteProposalRepository, 'mark_approved'))
    original = getattr(cls, method)
    def fail(self, *args):
        if failure.endswith('after'):
            original(self, *args)
        raise sqlite3.OperationalError('Injected failure')
    monkeypatch.setattr(cls, method, fail)
    connection.execute('BEGIN')
    connection.execute('CREATE TABLE caller_work (value TEXT)')
    with pytest.raises(sqlite3.OperationalError, match='Injected failure'):
        service.approve(proposal.proposal_id, 'Human')
    assert connection.in_transaction
    assert proposals.load(proposal.proposal_id) == proposal
    assert approvals.load(proposal.proposal_id) is None
    connection.commit()
    assert connection.execute('SELECT count(*) FROM caller_work').fetchone()[0] == 0


@pytest.mark.parametrize('statement', [
    "UPDATE human_approvals SET approver = 'Other'",
    "UPDATE human_approvals SET proposal_id = 'Other'",
    'DELETE FROM human_approvals',
    'INSERT OR REPLACE INTO human_approvals SELECT * FROM human_approvals',
    "INSERT OR REPLACE INTO human_approvals SELECT 'new-id', proposal_id, approved_at, approver FROM human_approvals",
    "INSERT OR REPLACE INTO human_approvals SELECT approval_id, 'other-proposal', approved_at, approver FROM human_approvals",
])
def test_sql_cannot_change_approval_evidence(database, statement):
    _, connection, _, approvals, proposal, service = database
    approval = service.approve(proposal.proposal_id, 'Human')
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
    assert approvals.load(proposal.proposal_id) == approval


@pytest.mark.parametrize('commit', [False, True])
def test_outer_transaction_remains_caller_owned(database, commit):
    path, connection, proposals, approvals, proposal, service = database
    connection.execute('BEGIN')
    approval = service.approve(proposal.proposal_id, 'Human')
    assert connection.in_transaction
    with sqlite3.connect(path) as observer:
        assert SQLiteProposalRepository(observer).load(proposal.proposal_id) == proposal
        assert SQLiteApprovalRepository(observer).load(proposal.proposal_id) is None
    if commit:
        connection.commit()
        assert proposals.load(proposal.proposal_id).status is ProposalStatus.APPROVED
        assert approvals.load(proposal.proposal_id) == approval
    else:
        connection.rollback()
        assert proposals.load(proposal.proposal_id) == proposal
        assert approvals.load(proposal.proposal_id) is None


@pytest.mark.parametrize('status', ['PENDING', 'APPROVED', 'REJECTED'])
def test_approved_proposals_are_terminal_in_sql(database, status):
    _, connection, proposals, _, proposal, service = database
    service.approve(proposal.proposal_id, 'Human')
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('UPDATE trade_proposals SET status = ?, rejection_reason = ?',
                           (status, 'reason' if status == 'REJECTED' else None))
    assert proposals.load(proposal.proposal_id).status is ProposalStatus.APPROVED


def test_approved_terms_remain_database_immutable(database):
    _, connection, proposals, _, proposal, service = database
    service.approve(proposal.proposal_id, 'Human')
    for column, value in [('symbol', 'MSFT'), ('price', '1'), ('quantity', 1),
                          ('portfolio_id', 'other'), ('rationale', 'changed')]:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'UPDATE trade_proposals SET {column} = ?', (value,))
    assert proposals.load(proposal.proposal_id) == replace(proposal, status=ProposalStatus.APPROVED)
