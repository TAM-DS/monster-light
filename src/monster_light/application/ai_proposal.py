"""Persist structured AI suggestions as untrusted, pending proposals only."""

from decimal import Decimal

from monster_light.application.proposal import ProposalOrigin, TradeProposal
from monster_light.application.trade_service import TradeSide
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository


class AIProposalService:
    def __init__(self, repository: SQLiteProposalRepository) -> None:
        self._repository = repository

    def create(
        self, *, portfolio_id: str, side: TradeSide, symbol: str,
        quantity: int, price: Decimal, rationale: str, grounding_evidence_id: str,
    ) -> TradeProposal:
        """Validate structure and save; economic validity belongs to execution."""
        if not isinstance(grounding_evidence_id, str) or not grounding_evidence_id.strip():
            raise ValueError("grounding_evidence_id must be a non-empty string")
        proposal = TradeProposal(
            portfolio_id=portfolio_id, side=side, symbol=symbol,
            quantity=quantity, price=price, rationale=rationale,
            origin=ProposalOrigin.AI, grounding_evidence_id=grounding_evidence_id,
        )
        self._repository.save(proposal)
        return proposal
