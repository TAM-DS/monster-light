"""Review a synthetic AI proposal and explicitly record human approval only."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from openai import OpenAI

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.model_proposal_adapter import ModelProposalAdapter
from monster_light.application.proposal import ProposalOrigin, ProposalStatus, TradeProposal
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository


def run_demo(
    client: OpenAI, *, read_input: Callable[[str], str] | None = None,
) -> tuple[TradeProposal, HumanApproval | None]:
    """Require explicit operator input; injected clients and input keep tests offline."""
    now = datetime.now(timezone.utc)
    evidence = MarketEvidence(
        evidence_id="demo-evidence-001", source="synthetic-demo",
        symbol="AAPL", price=Decimal("210.00"),
        observed_at=now, retrieved_at=now,
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        proposals = SQLiteProposalRepository(connection)
        proposals.initialize_schema()
        approvals = SQLiteApprovalRepository(connection)
        approvals.initialize_schema()
        adapter = ModelProposalAdapter(client, "gpt-5.6-luna", AIProposalService(proposals))
        proposal = adapter.propose(
            "Using only the trusted context, propose a small illustrative trade "
            "for testing. Do not claim approval or execution.",
            portfolio_id="demo-portfolio", evidence=evidence,
        )
        if (proposal.origin is not ProposalOrigin.AI
                or proposal.status is not ProposalStatus.PENDING
                or proposal.grounding_evidence_id != evidence.evidence_id
                or proposal.symbol != evidence.symbol
                or proposal.price != evidence.price
                or proposals.load(proposal.proposal_id) != proposal
                or approvals.load(proposal.proposal_id) is not None):
            raise RuntimeError("Human approval demo proposal invariant failed")

        print(
            "Human approval demo (synthetic-demo evidence)\n"
            f"proposal_id: {proposal.proposal_id}\n"
            f"origin: {proposal.origin.value}\n"
            f"status before approval: {proposal.status.value}\n"
            f"portfolio_id: {proposal.portfolio_id}\n"
            f"side: {proposal.side.value}\n"
            f"symbol: {proposal.symbol}\n"
            f"quantity: {proposal.quantity}\n"
            f"price: {proposal.price}\n"
            f"grounding_evidence_id: {proposal.grounding_evidence_id}\n"
            f"rationale (model-generated): {json.dumps(proposal.rationale)}\n"
            "approver: demo-human (caller-supplied metadata; not authenticated identity)"
        )
        try:
            answer = (read_input or input)("Type approve to record human approval: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "approve":
            print("Authority boundary: no human approval was recorded. "
                  "No execution was attempted.")
            return proposal, None

        approval = ApprovalService(proposals).approve(proposal.proposal_id, "demo-human")
        approved = proposals.load(proposal.proposal_id)
        if (approved.status is not ProposalStatus.APPROVED
                or approved != replace(proposal, status=ProposalStatus.APPROVED)
                or approved.price.as_tuple() != proposal.price.as_tuple()
                or approval.proposal_id != proposal.proposal_id
                or not approval.approval_id
                or approval.approver != "demo-human"
                or approvals.load(proposal.proposal_id) != approval):
            raise RuntimeError("Human approval demo approval invariant failed")

    print(
        f"status after approval: {approved.status.value}\n"
        f"approval_id: {approval.approval_id}\n"
        "Authority boundary: human approval was recorded. No execution was attempted."
    )
    return approved, approval


def main() -> None:
    with OpenAI(max_retries=0) as client:
        run_demo(client)


if __name__ == "__main__":
    main()
