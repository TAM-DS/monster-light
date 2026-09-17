"""Structured model suggestions have no authority beyond proposal creation."""

import json
import socket
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.audit import TradeAudit
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.model_proposal_adapter import (
    ModelGroundingMismatch, ModelProposalAdapter, ModelProposalCandidate,
)
from monster_light.application.proposal import ProposalOrigin, ProposalStatus
from monster_light.application.trade_service import TradeService, TradeSide
from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


def terms(**changes):
    return dict(
        portfolio_id="one", side="BUY", symbol=" aApL \t", quantity=500,
        price="123.4567890123456789012345678900",
        rationale=" 100% confident. Approved! Execute immediately. \n",
    ) | changes


def market_evidence(**changes):
    return MarketEvidence(**(dict(
        evidence_id="caller-evidence", source="caller source",
        symbol=terms()["symbol"], price=Decimal(terms()["price"]),
        observed_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        retrieved_at=datetime(2000, 1, 2, tzinfo=timezone.utc),
    ) | changes))


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.responses = self
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        # Exercise the same Pydantic JSON validation used by SDK parsing.
        parsed = kwargs["text_format"].model_validate_json(json.dumps(self.payload))
        return SimpleNamespace(output_parsed=parsed)


@pytest.fixture(autouse=True)
def no_network_or_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def database():
    with sqlite3.connect(":memory:") as connection:
        repository = SQLiteProposalRepository(connection)
        repository.initialize_schema()
        yield connection, repository


def assert_empty(database):
    connection, _ = database
    assert connection.execute("SELECT count(*) FROM trade_proposals").fetchone()[0] == 0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_terms_delegate_to_service_and_persist_pending_ai(database, side):
    _, repository = database
    client = FakeClient(terms(side=side))
    service = Mock(wraps=AIProposalService(repository), spec=AIProposalService)
    adapter = ModelProposalAdapter(client, "explicit-model", service)
    before = datetime.now(timezone.utc)
    proposal = adapter.propose("User intent", portfolio_id="one", evidence=market_evidence())

    service.create.assert_called_once_with(
        **(terms(side=TradeSide(side), price=Decimal(terms()["price"])))
    )
    call, = client.calls
    assert call["model"] == "explicit-model"
    assert call["text_format"] is ModelProposalCandidate
    assert call["input"][1] == {"role": "user", "content": "User intent"}
    context = json.loads(call["input"][0]["content"].split("\n", 1)[1])
    assert context == dict(
        portfolio_id="one", source="caller source", symbol=terms()["symbol"],
        price=terms()["price"], observed_at="2000-01-01T00:00:00+00:00",
        retrieved_at="2000-01-02T00:00:00+00:00",
    )
    assert proposal.portfolio_id == "one"
    assert repository.load(proposal.proposal_id) == proposal
    assert proposal.origin is ProposalOrigin.AI
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.symbol == terms()["symbol"]
    assert proposal.quantity == 500
    assert proposal.rationale == terms()["rationale"]
    assert isinstance(proposal.price, Decimal)
    assert proposal.price.as_tuple() == Decimal(terms()["price"]).as_tuple()
    assert UUID(proposal.proposal_id).version == 4
    assert before <= proposal.created_at <= datetime.now(timezone.utc)
    another = adapter.propose("More intent", portfolio_id="one", evidence=market_evidence())
    assert another.proposal_id != proposal.proposal_id


def test_schema_has_exactly_six_required_fields():
    schema = ModelProposalCandidate.model_json_schema()
    assert set(schema["properties"]) == set(terms())
    assert set(schema["required"]) == set(terms())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["price"]["type"] == "string"


@pytest.mark.parametrize("field,value", [
    ("proposal_id", "model-id"), ("created_at", "2000-01-01T00:00:00Z"),
    ("origin", "MANUAL"), ("status", "APPROVED"),
    ("approval_id", "approval"), ("market_evidence_id", "evidence"),
    ("audit_id", "audit"), ("audit", {"outcome": "ACCEPTED"}),
    ("audit_fields", {}), ("confidence", 1), ("unexpected", "extra"),
])
def test_extra_and_system_owned_fields_rejected_before_persistence(database, field, value):
    _, repository = database
    adapter = ModelProposalAdapter(
        FakeClient(terms(**{field: value})), "test", AIProposalService(repository),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.propose("intent", portfolio_id="one", evidence=market_evidence())
    assert_empty(database)


@pytest.mark.parametrize("field", list(terms()))
def test_every_field_is_required(database, field):
    _, repository = database
    payload = terms()
    del payload[field]
    adapter = ModelProposalAdapter(FakeClient(payload), "test", AIProposalService(repository))
    with pytest.raises(ValidationError, match="Field required"):
        adapter.propose("intent", portfolio_id="one", evidence=market_evidence())
    assert_empty(database)


@pytest.mark.parametrize("field,value", [
    *[("side", value) for value in ("BANANA", "buy", "", None, 1)],
    *[("quantity", value) for value in (0, -1, True, False, 1.5, "1", None)],
    *[("price", value) for value in (
        "invalid", "", "0", "-1", "NaN", "sNaN", "Infinity", "-Infinity",
        123.45, 1, True, None,
    )],
    *[(field, value) for field in ("portfolio_id", "symbol", "rationale")
      for value in ("", " \t", None, 7)],
])
def test_invalid_terms_fail_before_persistence(database, field, value):
    _, repository = database
    adapter = ModelProposalAdapter(
        FakeClient(terms(**{field: value})), "test", AIProposalService(repository),
    )
    with pytest.raises(ValueError):
        adapter.propose("intent", portfolio_id="one", evidence=market_evidence())
    assert_empty(database)


def test_no_parsed_result_is_explicit_failure(database):
    _, repository = database
    client = SimpleNamespace(responses=SimpleNamespace(
        parse=Mock(return_value=SimpleNamespace(output_parsed=None)),
    ))
    with pytest.raises(ValueError, match="no parsed proposal candidate"):
        ModelProposalAdapter(client, "test", AIProposalService(repository)).propose("intent", portfolio_id="one", evidence=market_evidence())
    assert_empty(database)


def test_unvalidated_candidate_instance_is_revalidated(database):
    _, repository = database
    candidate = ModelProposalCandidate.model_construct(**terms(quantity=True))
    client = SimpleNamespace(responses=SimpleNamespace(
        parse=Mock(return_value=SimpleNamespace(output_parsed=candidate)),
    ))
    with pytest.raises(ValidationError):
        ModelProposalAdapter(client, "test", AIProposalService(repository)).propose("intent", portfolio_id="one", evidence=market_evidence())
    assert_empty(database)


def test_model_failure_propagates_without_retry_or_persistence(database):
    _, repository = database
    parse = Mock(side_effect=RuntimeError("model unavailable"))
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    with pytest.raises(RuntimeError, match="model unavailable"):
        ModelProposalAdapter(client, "test", AIProposalService(repository)).propose("intent", portfolio_id="one", evidence=market_evidence())
    parse.assert_called_once()
    assert_empty(database)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_impossible_trade_can_be_pending_without_crossing_authority_boundary(
    database, monkeypatch, side,
):
    connection, repository = database
    portfolios = SQLitePortfolioRepository(connection)
    portfolios.initialize_schema()
    portfolios.save("one", Portfolio.restore(Decimal("0"), {"AAPL": 2}))
    SQLiteApprovalRepository(connection).initialize_schema()
    SQLiteAuditRepository(connection).initialize_schema()
    SQLiteMarketEvidenceRepository(connection).initialize_schema()
    evidence = market_evidence(symbol="AAPL", price=Decimal("210"))
    before = list(connection.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("Model adapter crossed the proposal boundary")

    def authorize(action, table, *args):
        if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT,
                      sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if table != "trade_proposals":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with monkeypatch.context() as patch:
        for cls, methods in (
            (SQLitePortfolioRepository, ("load", "save")),
            (Portfolio, ("buy", "sell", "quantity_for", "restore")),
            (TradeService, ("execute",)),
            (ApprovalService, ("approve",)),
            (HumanApproval, ("__init__",)),
            (TradeAudit, ("__init__",)),
            (MarketEvidence, ("__init__", "validate_freshness")),
            (SQLiteApprovalRepository, ("load", "append")),
            (SQLiteAuditRepository, ("append",)),
            (SQLiteMarketEvidenceRepository, ("load", "append")),
            (SQLiteProposalRepository, ("mark_approved", "mark_executed")),
        ):
            for method in methods:
                patch.setattr(cls, method, forbidden)
        connection.set_authorizer(authorize)
        try:
            proposal = ModelProposalAdapter(
                FakeClient(terms(side=side, symbol="AAPL", price="210")), "test",
                AIProposalService(repository),
            ).propose("Trade 500 AAPL", portfolio_id="one", evidence=evidence)
        finally:
            connection.set_authorizer(None)

    assert proposal.status is ProposalStatus.PENDING
    assert proposal.origin is ProposalOrigin.AI
    assert proposal.quantity == 500
    assert repository.load(proposal.proposal_id) == proposal
    assert portfolios.load("one").quantity_for("AAPL") == 2
    assert portfolios.load("one").cash == Decimal("0")
    after = [line for line in connection.iterdump()
             if not line.startswith('INSERT INTO "trade_proposals"')]
    assert after == before


@pytest.mark.parametrize("changes", [
    {"portfolio_id": "two"}, {"portfolio_id": "one "},
    {"symbol": "MSFT"}, {"symbol": "AAPL"}, {"symbol": " aapl \t"},
    {"symbol": "aApL"}, {"symbol": " aApL "},
    {"price": "123.4567890123456789012345678901"},
])
def test_grounding_mismatch_never_calls_creation(database, changes):
    _, repository = database
    service = Mock(wraps=AIProposalService(repository), spec=AIProposalService)
    adapter = ModelProposalAdapter(FakeClient(terms(**changes)), "test", service)
    with pytest.raises(ModelGroundingMismatch):
        adapter.propose("intent", portfolio_id="one", evidence=market_evidence())
    service.create.assert_not_called()
    assert_empty(database)


@pytest.mark.parametrize("price", ["210", "210.00", "2.10E+2", "210.0000000000000000001"])
def test_decimal_grounding_compares_exact_values(database, price):
    _, repository = database
    service = Mock(wraps=AIProposalService(repository), spec=AIProposalService)
    adapter = ModelProposalAdapter(FakeClient(terms(price=price)), "test", service)
    evidence = market_evidence(price=Decimal("210.0"))
    if Decimal(price) == evidence.price:
        proposal = adapter.propose("intent", portfolio_id="one", evidence=evidence)
        assert proposal.price == evidence.price
        assert proposal.status is ProposalStatus.PENDING
        assert repository.load(proposal.proposal_id).price == evidence.price
    else:
        with pytest.raises(ModelGroundingMismatch):
            adapter.propose("intent", portfolio_id="one", evidence=evidence)
        service.create.assert_not_called()
        assert_empty(database)


def test_caller_portfolio_id_is_preserved_exactly(database):
    _, repository = database
    portfolio_id = " Portfolio One \t"
    adapter = ModelProposalAdapter(
        FakeClient(terms(portfolio_id=portfolio_id)), "test", AIProposalService(repository),
    )
    proposal = adapter.propose("intent", portfolio_id=portfolio_id, evidence=market_evidence())
    assert proposal.portfolio_id == portfolio_id
    assert repository.load(proposal.proposal_id).portfolio_id == portfolio_id


def test_stale_evidence_is_context_without_freshness_evaluation(database, monkeypatch):
    _, repository = database
    evidence = market_evidence()
    freshness = Mock(side_effect=AssertionError("Freshness belongs downstream"))
    monkeypatch.setattr(MarketEvidence, "validate_freshness", freshness)
    client = FakeClient(terms(rationale="100% confident. Approved. Execute immediately."))
    proposal = ModelProposalAdapter(client, "test", AIProposalService(repository)).propose(
        "Use this old observation", portfolio_id="one", evidence=evidence,
    )
    freshness.assert_not_called()
    context = json.loads(client.calls[0]["input"][0]["content"].split("\n", 1)[1])
    assert context["observed_at"] == evidence.observed_at.isoformat()
    assert context["retrieved_at"] == evidence.retrieved_at.isoformat()
    assert proposal.rationale == "100% confident. Approved. Execute immediately."
    assert proposal.status is ProposalStatus.PENDING


def test_banana_fails_at_trade_side_boundary(database):
    _, repository = database
    service = Mock(wraps=AIProposalService(repository), spec=AIProposalService)
    adapter = ModelProposalAdapter(FakeClient(terms(side="BANANA")), "test", service)
    with pytest.raises(ValueError, match="'BANANA' is not a valid TradeSide"):
        adapter.propose("intent", portfolio_id="one", evidence=market_evidence())
    service.create.assert_not_called()
    assert_empty(database)
