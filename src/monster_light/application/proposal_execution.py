"""Execute approved immutable terms through the existing trusted trade service."""

from monster_light.application.audit import AuditOrigin, ExecutionAuditContext
from monster_light.application.proposal import (
    ProposalAlreadyExecuted, ProposalNotApproved, ProposalStatus,
)
from monster_light.application.trade_service import TradeRequest, TradeService
from monster_light.domain.portfolio import Portfolio, PortfolioError
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import PortfolioNotFound, SQLitePortfolioRepository
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint


class HumanApprovalRequired(ValueError):
    """An APPROVED status alone is not human approval evidence."""


class ApprovedProposalExecutionService:
    """Share one SQLite connection for trade, audit, and proposal atomicity.

    Caller transactions retain ownership, including durability of rejected
    audit evidence. SQLite locking governs concurrent execution attempts.
    """

    def __init__(self, repository: SQLiteProposalRepository) -> None:
        self._repository = repository

    def execute(self, proposal_id: str) -> Portfolio:
        connection = self._repository.connection
        rejection = None
        with sqlite_savepoint(connection):
            proposal = self._repository.load(proposal_id)
            if proposal.status is ProposalStatus.EXECUTED:
                raise ProposalAlreadyExecuted(proposal_id)
            if proposal.status is not ProposalStatus.APPROVED:
                raise ProposalNotApproved(proposal_id)
            approvals = SQLiteApprovalRepository(connection)
            approvals.initialize_schema()
            approval = approvals.load(proposal_id)
            if approval is None:
                raise HumanApprovalRequired(proposal_id)
            request = TradeRequest(
                portfolio_id=proposal.portfolio_id, side=proposal.side,
                symbol=proposal.symbol, quantity=proposal.quantity, price=proposal.price,
            )
            try:
                portfolio = TradeService(SQLitePortfolioRepository(connection)).execute(
                    request,
                    audit_context=ExecutionAuditContext(
                        origin=AuditOrigin.APPROVED_PROPOSAL,
                        proposal_id=proposal.proposal_id,
                        approval_id=approval.approval_id,
                    ),
                )
            except (PortfolioError, PortfolioNotFound) as error:
                rejection = error
            else:
                self._repository.mark_executed(proposal_id)
        # TradeService has already rolled back mutations on rejection. Release
        # our savepoint before re-raising so its REJECTED audit is retained.
        if rejection is not None:
            raise rejection
        return portfolio
