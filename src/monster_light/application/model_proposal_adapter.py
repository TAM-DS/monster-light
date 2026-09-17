"""Convert untrusted structured model candidates into pending AI proposals."""

from decimal import Decimal, InvalidOperation

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.proposal import TradeProposal
from monster_light.application.trade_service import TradeSide


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

    def propose(self, input_text: str) -> TradeProposal:
        response = self._client.responses.parse(
            model=self._model, input=input_text, text_format=ModelProposalCandidate,
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
        return self._proposal_service.create(
            portfolio_id=candidate.portfolio_id, side=side, symbol=candidate.symbol,
            quantity=candidate.quantity, price=price, rationale=candidate.rationale,
        )
