"""One synthetic proposal through human consent and governed execution."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from openai import OpenAI

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.model_proposal_adapter import ModelProposalAdapter
from monster_light.application.proposal import ProposalOrigin, ProposalStatus, TradeProposal
from monster_light.application.proposal_execution import ApprovedProposalExecutionService
from monster_light.application.trade_service import TradeSide
from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_demo(
    client: OpenAI, *, read_input: Callable[[str], str] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[TradeProposal, HumanApproval | None]:
    """Use production services; client, operator input and UTC clock are injectable."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        portfolios = SQLitePortfolioRepository(connection)
        proposals = SQLiteProposalRepository(connection)
        approvals = SQLiteApprovalRepository(connection)
        evidence = SQLiteMarketEvidenceRepository(connection)
        portfolios.initialize_schema()
        proposals.initialize_schema()
        approvals.initialize_schema()
        evidence.initialize_schema()
        SQLiteAuditRepository(connection).initialize_schema()
        # Initial synthetic funding only. Execution owns subsequent mutations.
        portfolios.save("demo-governed", Portfolio(Decimal("1000.00")))
        before = portfolios.load("demo-governed")
        now = clock()
        grounding = MarketEvidence(
            evidence_id="grounding-evidence-e2e", source="synthetic-grounding-demo",
            symbol="AAPL", price=Decimal("210.00"), observed_at=now, retrieved_at=now,
        )
        evidence.append(grounding)
        proposal = ModelProposalAdapter(
            client, "gpt-5.6-luna", AIProposalService(proposals),
        ).propose(
            "Using only the trusted context, propose a single illustrative BUY "
            "of exactly one AAPL share. Do not claim approval or execution.",
            portfolio_id="demo-governed", evidence=grounding,
        )
        if (proposal.origin is not ProposalOrigin.AI
                or proposal.status is not ProposalStatus.PENDING
                or proposal.portfolio_id != "demo-governed"
                or proposal.side is not TradeSide.BUY
                or proposal.quantity != 1
                or proposal.symbol != "AAPL"
                or proposal.price != Decimal("210.00")
                or proposal.grounding_evidence_id != grounding.evidence_id
                or proposals.load(proposal.proposal_id) != proposal
                or approvals.load(proposal.proposal_id) is not None):
            raise RuntimeError("Governed workflow proposal invariant failed")
        print(
            "Synthetic trading demo; no broker or real money.\n"
            "STAGE 1 — AI PROPOSAL\n"
            f"proposal_id: {proposal.proposal_id}\n"
            "origin: AI\nstatus: PENDING\n"
            f"portfolio_id: {proposal.portfolio_id}\n"
            f"side: {proposal.side.value}\nsymbol: {proposal.symbol}\n"
            f"quantity: {proposal.quantity}\nprice: {proposal.price}\n"
            f"grounding_evidence_id: {proposal.grounding_evidence_id}\n"
            f"rationale (model-generated): {json.dumps(proposal.rationale)}\n"
            "AI proposes; it cannot authorize or execute.\n"
            "approver: demo-human (caller-supplied metadata; not authenticated identity)"
        )
        try:
            answer = (read_input or input)("Type approve to record human approval: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "approve":
            print("status: PENDING\nNo human approval was recorded. No execution was attempted.")
            return proposal, None

        approval = ApprovalService(proposals).approve(proposal.proposal_id, "demo-human")
        approved = proposals.load(proposal.proposal_id)
        if (approved != replace(proposal, status=ProposalStatus.APPROVED)
                or approved.price.as_tuple() != proposal.price.as_tuple()
                or approval.proposal_id != proposal.proposal_id
                or approval.approver != "demo-human"
                or approvals.load(proposal.proposal_id) != approval
                or evidence.load(grounding.evidence_id) != grounding):
            raise RuntimeError("Governed workflow approval invariant failed")
        print(
            "\nSTAGE 2 — HUMAN AUTHORIZATION\nstatus: APPROVED\n"
            f"proposal_id: {approved.proposal_id}\napproval_id: {approval.approval_id}\n"
            "Human approval permits an attempt; deterministic controls decide execution."
        )
        now = clock()
        execution_evidence = MarketEvidence(
            evidence_id="execution-evidence-e2e", source="synthetic-execution-demo",
            symbol="AAPL", price=Decimal("210.00"), observed_at=now, retrieved_at=now,
        )
        evidence.append(execution_evidence)
        ApprovedProposalExecutionService(
            proposals, maximum_age=timedelta(minutes=5), clock=clock,
        ).execute(proposal.proposal_id, execution_evidence.evidence_id)
        executed = proposals.load(proposal.proposal_id)
        after = portfolios.load("demo-governed")
        # Production has no audit read API; read its generated record directly.
        audit, = connection.execute("SELECT * FROM trade_audits").fetchall()
        if (executed != replace(proposal, status=ProposalStatus.EXECUTED)
                or executed.price.as_tuple() != proposal.price.as_tuple()
                or before.cash != Decimal("1000.00") or before.positions != {}
                or after.cash != Decimal("790.00") or after.positions != {"AAPL": 1}
                or audit["outcome"] != "ACCEPTED"
                or audit["origin"] != "APPROVED_PROPOSAL"
                or audit["proposal_id"] != proposal.proposal_id
                or audit["approval_id"] != approval.approval_id
                or audit["market_evidence_id"] != execution_evidence.evidence_id
                or grounding.evidence_id == execution_evidence.evidence_id
                or evidence.load(grounding.evidence_id) != grounding
                or evidence.load(execution_evidence.evidence_id) != execution_evidence
                or approvals.load(proposal.proposal_id) != approval):
            raise RuntimeError("Governed workflow execution invariant failed")
        print(
            "\nSTAGE 3 — DETERMINISTIC EXECUTION\nstatus: EXECUTED\n"
            f"proposal_id: {executed.proposal_id}\n"
            f"cash: {before.cash} -> {after.cash}\n"
            f"AAPL quantity: {before.quantity_for('AAPL')} -> {after.quantity_for('AAPL')}\n"
            "\nSTAGE 4 — EVIDENCE\n"
            f"grounding_evidence_id: {executed.grounding_evidence_id}\n"
            f"execution_evidence_id: {audit['market_evidence_id']}\n"
            f"audit_id: {audit['audit_id']}\naudit outcome: {audit['outcome']}\n"
            f"audit origin: {audit['origin']}\n"
            f"audit proposal_id: {audit['proposal_id']}\n"
            f"audit approval_id: {audit['approval_id']}\n"
            "Audit provenance is append-only in this disposable in-memory database.\n"
            'Grounding evidence answers:\n"What informed the recommendation?"\n'
            'Execution evidence answers:\n"What did the system verify when action was attempted?"\n'
            "\nGovernance proof:\nAI proposed.\nA human authorized.\n"
            "Deterministic controls verified current reality.\nOnly then did execution occur."
        )
        return executed, approval


def main() -> None:
    with OpenAI(max_retries=0) as client:
        run_demo(client)


if __name__ == "__main__":
    main()
