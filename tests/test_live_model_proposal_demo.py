"""The live demo is testable offline and stops at a grounded pending proposal."""

import json
import socket
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from monster_light.application.proposal import ProposalOrigin, ProposalStatus
from monster_light.demo import live_model_proposal as demo


class FakeClient:
    def __init__(self):
        self.responses = self
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        candidate = kwargs["text_format"].model_validate_json(json.dumps(dict(
            portfolio_id="demo-portfolio", side="BUY", symbol="AAPL",
            quantity=1, price="210.00", rationale="Illustrative synthetic trade.",
        )))
        return SimpleNamespace(output_parsed=candidate)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Demo tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_demo_produces_grounded_pending_ai_proposal(capsys):
    client = FakeClient()
    before = datetime.now(timezone.utc)
    proposal = demo.run_demo(client)
    after = datetime.now(timezone.utc)

    assert proposal.origin is ProposalOrigin.AI
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.grounding_evidence_id == "demo-evidence-001"
    assert proposal.symbol == "AAPL"
    assert proposal.price == Decimal("210.00")
    call, = client.calls
    assert call["model"] == "gpt-5.6-luna"
    context = json.loads(call["input"][0]["content"].split("\n", 1)[1])
    assert context["source"] == "synthetic-demo"
    assert context["symbol"] == "AAPL"
    assert context["price"] == "210.00"
    observed = datetime.fromisoformat(context["observed_at"])
    assert observed.tzinfo == timezone.utc
    assert before <= observed <= after
    assert context["observed_at"] == context["retrieved_at"]
    assert call["input"][1]["content"] == (
        "Using only the trusted context, propose a small illustrative trade "
        "for testing. Do not claim approval or execution."
    )
    assert capsys.readouterr().out == (
        "Live model proposal demo (synthetic-demo evidence)\n"
        f"proposal_id: {proposal.proposal_id}\n"
        "origin: AI\nstatus: PENDING\nportfolio_id: demo-portfolio\n"
        "side: BUY\nsymbol: AAPL\nquantity: 1\nprice: 210.00\n"
        "grounding_evidence_id: demo-evidence-001\n"
        "rationale: Illustrative synthetic trade.\n"
        "Authority boundary: proposal is PENDING. "
        "No approval or execution was attempted.\n"
    )


@pytest.mark.parametrize("changes", [
    {"origin": ProposalOrigin.MANUAL},
    {"status": ProposalStatus.APPROVED},
    {"grounding_evidence_id": "wrong-evidence"},
    {"symbol": "MSFT"},
    {"price": Decimal("211.00")},
])
def test_demo_rejects_broken_invariants_before_output(monkeypatch, capsys, changes):
    propose = demo.ModelProposalAdapter.propose

    def invalid_proposal(self, *args, **kwargs):
        proposal = propose(self, *args, **kwargs)
        return SimpleNamespace(**(vars(proposal) | changes))

    monkeypatch.setattr(demo.ModelProposalAdapter, "propose", invalid_proposal)
    with pytest.raises(RuntimeError, match="proposal invariant failed"):
        demo.run_demo(FakeClient())
    assert capsys.readouterr().out == ""


def test_main_constructs_client_without_retries(monkeypatch):
    client = FakeClient()
    manager = MagicMock()
    manager.__enter__.return_value = client
    factory = Mock(return_value=manager)
    monkeypatch.setattr(demo, "OpenAI", factory)
    demo.main()
    factory.assert_called_once_with(max_retries=0)
    assert len(client.calls) == 1
