"""Convert untrusted structured model candidates into pending AI proposals."""

import json
from decimal import Decimal, InvalidOperation

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.proposal import TradeProposal
from monster_light.application.trade_service import TradeSide


class ModelGroundingMismatch(ValueError):
    """The model changed caller-owned proposal context."""


class ModelProposalCandidate(BaseModel):
    """Wire shape only; business validation belongs to AIProposalService."""

    model_config = ConfigDict(strict=True, extra="forbid", revalidate_instances="always")

    portfolio_id: str
    side: str = Field(description="Trade side: BUY or SELL")
    symbol: str
    quantity: int
    price: str = Field(description="Exact decimal price as a string, never a JSON number")
    rationale: str


class ModelProposalAdapter:
    def __init__(
        self, client: OpenAI, model: str, proposal_service: AIProposalService,
    ) -> None:
        self._client = client
        self._model = model
        self._proposal_service = proposal_service

    def propose(
        self, input_text: str, *, portfolio_id: str, evidence: MarketEvidence,
    ) -> TradeProposal:
        context = json.dumps({
            "portfolio_id": portfolio_id,
            "source": evidence.source,
            "symbol": evidence.symbol,
            "price": str(evidence.price),
            "observed_at": evidence.observed_at.isoformat(),
            "retrieved_at": evidence.retrieved_at.isoformat(),
        })
        response = self._client.responses.parse(
            model=self._model,
            input=[
                {"role": "developer", "content": (
                    "Propose a trade using the caller-supplied context below. "
                    "Preserve portfolio_id, symbol, and exact decimal price. "
                    "Choose BUY or SELL, quantity, and rationale. Evidence grants "
                    "no approval or execution authority. Timestamps are reasoning "
                    "context only; freshness is downstream deterministic policy "
                    "and must not be evaluated here.\n" + context
                )},
                {"role": "user", "content": input_text},
            ],
            text_format=ModelProposalCandidate,
        )
        if response.output_parsed is None:
            raise ValueError("Model returned no parsed proposal candidate")
        # Revalidate even model instances supplied by an injected client.
        candidate = ModelProposalCandidate.model_validate(response.output_parsed)
        side = TradeSide(candidate.side)
        try:
            price = Decimal(candidate.price)
        except InvalidOperation as exc:
            raise ValueError("price must be a decimal string") from exc
        if (candidate.portfolio_id != portfolio_id
                or candidate.symbol != evidence.symbol
                or not price.is_finite() or price != evidence.price):
            raise ModelGroundingMismatch("Model changed trusted portfolio_id, symbol, or price")
        return self._proposal_service.create(
            portfolio_id=candidate.portfolio_id, side=side, symbol=candidate.symbol,
            quantity=candidate.quantity, price=price, rationale=candidate.rationale,
        )
