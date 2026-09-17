"""Propose one illustrative trade using explicitly synthetic evidence."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal

from openai import OpenAI

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.model_proposal_adapter import ModelProposalAdapter
from monster_light.application.proposal import ProposalOrigin, ProposalStatus, TradeProposal
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository


def run_demo(client: OpenAI, model: str = "gpt-5.6-luna") -> TradeProposal:
    """Create and display a pending proposal; the caller supplies the client."""
    now = datetime.now(timezone.utc)
    evidence = MarketEvidence(
        evidence_id="demo-evidence-001", source="synthetic-demo",
        symbol="AAPL", price=Decimal("210.00"),
        observed_at=now, retrieved_at=now,
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        repository = SQLiteProposalRepository(connection)
        repository.initialize_schema()
        adapter = ModelProposalAdapter(client, model, AIProposalService(repository))
        proposal = adapter.propose(
            "Using only the trusted context, propose a small illustrative trade "
            "for testing. Do not claim approval or execution.",
            portfolio_id="demo-portfolio", evidence=evidence,
        )
        if (proposal.origin is not ProposalOrigin.AI
                or proposal.status is not ProposalStatus.PENDING
                or proposal.grounding_evidence_id != evidence.evidence_id
                or proposal.symbol != evidence.symbol
                or proposal.price != evidence.price):
            raise RuntimeError("Live model demo proposal invariant failed")

    print(
        "Live model proposal demo (synthetic-demo evidence)\n"
        f"proposal_id: {proposal.proposal_id}\n"
        f"origin: {proposal.origin.value}\n"
        f"status: {proposal.status.value}\n"
        f"portfolio_id: {proposal.portfolio_id}\n"
        f"side: {proposal.side.value}\n"
        f"symbol: {proposal.symbol}\n"
        f"quantity: {proposal.quantity}\n"
        f"price: {proposal.price}\n"
        f"grounding_evidence_id: {proposal.grounding_evidence_id}\n"
        f"rationale: {proposal.rationale}\n"
        "Authority boundary: proposal is PENDING. "
        "No approval or execution was attempted."
    )
    return proposal


def main() -> None:
    with OpenAI(max_retries=0) as client:
        run_demo(client)


if __name__ == "__main__":
    main()
