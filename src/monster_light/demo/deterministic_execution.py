"""Offline execution examples with synthetic human approval fixtures."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.proposal import ProposalStatus, TradeProposal
from monster_light.application.proposal_execution import ApprovedProposalExecutionService
from monster_light.application.trade_service import TradeSide
from monster_light.domain.portfolio import InsufficientCash, Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


@dataclass(frozen=True)
class CaseResult:
    before: TradeProposal
    after: TradeProposal
    approval: HumanApproval
    portfolio_before: Portfolio
    portfolio_after: Portfolio
    audit: sqlite3.Row
    error: InsufficientCash | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_case(
    connection: sqlite3.Connection, *, portfolio_id: str, cash: Decimal,
    grounding_id: str, execution_id: str, expected_status: ProposalStatus,
) -> CaseResult:
    portfolios = SQLitePortfolioRepository(connection)
    proposals = SQLiteProposalRepository(connection)
    evidence = SQLiteMarketEvidenceRepository(connection)
    # Initial synthetic state only; all subsequent mutations belong to execution.
    portfolios.save(portfolio_id, Portfolio(cash))
    now = _utc_now()
    evidence.append(MarketEvidence(
        evidence_id=grounding_id, source="synthetic-grounding-demo",
        symbol="AAPL", price=Decimal("210.00"), observed_at=now, retrieved_at=now,
    ))
    proposal = AIProposalService(proposals).create(
        portfolio_id=portfolio_id, side=TradeSide.BUY, symbol="AAPL",
        quantity=1, price=Decimal("210.00"),
        rationale="Synthetic recommendation fixture: buy one AAPL share.",
        grounding_evidence_id=grounding_id,
    )
    approval = ApprovalService(proposals).approve(
        proposal.proposal_id, "demo-human-fixture",
    )
    before = proposals.load(proposal.proposal_id)
    if (before != replace(proposal, status=ProposalStatus.APPROVED)
            or SQLiteApprovalRepository(connection).load(proposal.proposal_id) != approval):
        raise RuntimeError("Expected persisted human approval before execution")
    portfolio_before = portfolios.load(portfolio_id)
    now = _utc_now()
    evidence.append(MarketEvidence(
        evidence_id=execution_id, source="synthetic-execution-demo",
        symbol="AAPL", price=Decimal("210.00"), observed_at=now, retrieved_at=now,
    ))
    execution = ApprovedProposalExecutionService(
        proposals, maximum_age=timedelta(minutes=5), clock=_utc_now,
    )
    error = None
    try:
        execution.execute(proposal.proposal_id, execution_id)
    except InsufficientCash as rejection:
        error = rejection

    after = proposals.load(proposal.proposal_id)
    portfolio_after = portfolios.load(portfolio_id)
    # There is no audit read repository API; read the production audit directly.
    audit, = connection.execute(
        "SELECT * FROM trade_audits WHERE proposal_id = ?", (proposal.proposal_id,),
    ).fetchall()
    accepted = expected_status is ProposalStatus.EXECUTED
    if (after != replace(before, status=expected_status)
            or before.grounding_evidence_id != grounding_id
            or grounding_id == execution_id
            or (error is None) != accepted
            or audit["outcome"] != ("ACCEPTED" if accepted else "REJECTED")
            or audit["reason_code"] != ("TradeExecuted" if accepted else "InsufficientCash")
            or audit["origin"] != "APPROVED_PROPOSAL"
            or audit["proposal_id"] != proposal.proposal_id
            or audit["approval_id"] != approval.approval_id
            or audit["market_evidence_id"] != execution_id
            or portfolio_after.cash != (cash - Decimal("210.00") if accepted else cash)
            or portfolio_after.positions != ({"AAPL": 1} if accepted else {})
            or SQLiteApprovalRepository(connection).load(proposal.proposal_id) != approval):
        raise RuntimeError("Deterministic execution demo invariant failed")
    return CaseResult(before, after, approval, portfolio_before, portfolio_after, audit, error)


def _print_case(title: str, result: CaseResult) -> None:
    print(
        f"\n{title}\n"
        f"proposal_id: {result.before.proposal_id}\n"
        f"approval_id: {result.approval.approval_id}\n"
        f"status before execution: {result.before.status.value}\n"
        f"status after attempt: {result.after.status.value}\n"
        f"symbol: {result.before.symbol}\n"
        f"quantity: {result.before.quantity}\n"
        f"price: {result.before.price}\n"
        f"grounding_evidence_id: {result.after.grounding_evidence_id}\n"
        f"execution_evidence_id: {result.audit['market_evidence_id']}\n"
        f"audit outcome: {result.audit['outcome']}\n"
        f"audit origin: {result.audit['origin']}\n"
        f"reason: {result.audit['reason_code']}\n"
        f"portfolio_id: {result.before.portfolio_id}\n"
        f"cash: {result.portfolio_before.cash} -> {result.portfolio_after.cash}\n"
        f"AAPL quantity: {result.portfolio_before.quantity_for('AAPL')} -> "
        f"{result.portfolio_after.quantity_for('AAPL')}"
    )


def run_demo() -> tuple[CaseResult, CaseResult]:
    """Run two fixed scenarios on one disposable production repository store.

    Approval is a synthetic human decision fixture, not authenticated operator
    input. Production services generate their usual UUIDs and UTC timestamps.
    """
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        SQLitePortfolioRepository(connection).initialize_schema()
        SQLiteProposalRepository(connection).initialize_schema()
        SQLiteApprovalRepository(connection).initialize_schema()
        SQLiteMarketEvidenceRepository(connection).initialize_schema()
        SQLiteAuditRepository(connection).initialize_schema()
        success = _run_case(
            connection, portfolio_id="demo-funded", cash=Decimal("1000.00"),
            grounding_id="grounding-evidence-001", execution_id="execution-evidence-001",
            expected_status=ProposalStatus.EXECUTED,
        )
        failure = _run_case(
            connection, portfolio_id="demo-insufficient-cash", cash=Decimal("100.00"),
            grounding_id="grounding-evidence-002", execution_id="execution-evidence-002",
            expected_status=ProposalStatus.APPROVED,
        )
    print("Synthetic offline demo; human approvals are fixtures recorded by ApprovalService.")
    _print_case("Case 1: APPROVED -> deterministic validation passes -> EXECUTED", success)
    _print_case("Case 2: APPROVED -> deterministic validation fails -> remains APPROVED", failure)
    print(
        "\nAuthority boundary: human approval permitted an execution attempt. "
        "Deterministic validation still controlled the outcome."
    )
    return success, failure


def main() -> None:
    run_demo()


if __name__ == "__main__":
    main()
