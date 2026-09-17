"""Offline integration proof of the demo's authority boundaries."""

import json
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from monster_light.application.model_proposal_adapter import ModelGroundingMismatch
from monster_light.application.proposal import ProposalStatus
from monster_light.application.trade_service import TradeService
from monster_light.demo import governed_workflow as demo


class FakeClient:
    def __init__(self, **changes):
        self.responses = self
        self.calls = []
        self.candidate = dict(
            portfolio_id="demo-governed", side="BUY", symbol="AAPL",
            quantity=1, price="210.00", rationale="Illustrative synthetic trade.",
        ) | changes

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.candidate)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Demo tests must stay offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def store(monkeypatch):
    connect = sqlite3.connect
    connections = []

    def capture(database):
        assert database == ":memory:"
        connection = connect(database)
        connections.append(connection)
        return connection

    monkeypatch.setattr(demo.sqlite3, "connect", capture)
    return connections


def test_same_proposal_through_real_services(monkeypatch, capsys, store):
    client = FakeClient()
    consent = []
    snapshots = []
    approve = demo.ApprovalService.approve
    execute = demo.ApprovedProposalExecutionService.execute
    trade = TradeService.execute
    trades = []
    start = datetime.now(timezone.utc)
    # Human deliberation can outlast grounding freshness; execution gets fresh evidence.
    clock = Mock(side_effect=[start, start + timedelta(minutes=10), start + timedelta(minutes=10)])

    def human_input(prompt):
        connection, = store
        pending, = connection.execute("SELECT proposal_id FROM trade_proposals").fetchall()
        proposal = demo.SQLiteProposalRepository(connection).load(pending[0])
        snapshots.append(proposal)
        assert proposal.status is ProposalStatus.PENDING
        assert connection.execute("SELECT count(*) FROM human_approvals").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM market_evidence").fetchone()[0] == 1
        assert demo.SQLitePortfolioRepository(connection).load("demo-governed").cash == Decimal("1000.00")
        output = capsys.readouterr().out
        assert "STAGE 1 — AI PROPOSAL" in output
        assert "status: PENDING" in output
        assert "side: BUY\nsymbol: AAPL\nquantity: 1\nprice: 210.00" in output
        assert "caller-supplied metadata; not authenticated identity" in output
        assert "AI proposes; it cannot authorize or execute." in output
        assert prompt == "Type approve to record human approval: "
        consent.append("approve")
        return "approve"

    def observe_approval(self, proposal_id, approver):
        assert consent == ["approve"]
        assert proposal_id == snapshots[0].proposal_id
        return approve(self, proposal_id, approver)

    def observe_execution(self, proposal_id, market_evidence_id):
        connection, = store
        approved = demo.SQLiteProposalRepository(connection).load(proposal_id)
        snapshots.append(approved)
        assert approved == replace(snapshots[0], status=ProposalStatus.APPROVED)
        approval = demo.SQLiteApprovalRepository(connection).load(proposal_id)
        assert approval.proposal_id == proposal_id
        assert approval.approver == "demo-human"
        evidence = demo.SQLiteMarketEvidenceRepository(connection).load(market_evidence_id)
        assert evidence.evidence_id == "execution-evidence-e2e"
        assert evidence.source == "synthetic-execution-demo"
        assert evidence.observed_at == evidence.retrieved_at == start + timedelta(minutes=10)
        assert evidence.observed_at > approval.approved_at
        grounding = demo.SQLiteMarketEvidenceRepository(connection).load("grounding-evidence-e2e")
        assert grounding.observed_at == grounding.retrieved_at == start
        assert grounding.evidence_id != evidence.evidence_id
        result = execute(self, proposal_id, market_evidence_id)
        assert demo.SQLiteMarketEvidenceRepository(connection).load(grounding.evidence_id) == grounding
        audit, = connection.execute("SELECT * FROM trade_audits").fetchall()
        assert audit["outcome"] == "ACCEPTED"
        assert audit["origin"] == "APPROVED_PROPOSAL"
        assert audit["proposal_id"] == proposal_id
        assert audit["approval_id"] == approval.approval_id
        assert audit["market_evidence_id"] == market_evidence_id
        assert UUID(audit["audit_id"]).version == 4
        assert (audit["cash_before"], audit["cash_after"]) == ("1000.00", "790.00")
        assert (audit["quantity_before"], audit["quantity_after"]) == (0, 1)
        assert result.cash == Decimal("790.00")
        assert result.positions == {"AAPL": 1}
        assert connection.execute("SELECT count(*) FROM human_approvals").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE trade_audits SET outcome = 'REJECTED'")
        return result

    def observe_trade(self, request, **kwargs):
        assert len(snapshots) == 2
        assert kwargs["audit_context"].proposal_id == snapshots[0].proposal_id
        trades.append(request)
        return trade(self, request, **kwargs)

    approval_spy = Mock(side_effect=observe_approval)
    execution_spy = Mock(side_effect=observe_execution)
    monkeypatch.setattr(demo.ApprovalService, "approve", lambda self, *args: approval_spy(self, *args))
    monkeypatch.setattr(demo.ApprovedProposalExecutionService, "execute", lambda self, *args: execution_spy(self, *args))
    monkeypatch.setattr(TradeService, "execute", observe_trade)
    executed, approval = demo.run_demo(client, read_input=human_input, clock=clock)
    assert approval_spy.call_count == execution_spy.call_count == len(trades) == 1
    assert executed == replace(snapshots[0], status=ProposalStatus.EXECUTED)
    assert executed.price.as_tuple() == snapshots[0].price.as_tuple()
    assert executed.grounding_evidence_id == "grounding-evidence-e2e"
    assert approval.proposal_id == executed.proposal_id
    assert UUID(approval.approval_id).version == 4
    call, = client.calls
    context = json.loads(call["input"][0]["content"].split("\n", 1)[1])
    assert context["source"] == "synthetic-grounding-demo"
    assert context["observed_at"] == context["retrieved_at"] == start.isoformat()
    assert context["symbol"] == "AAPL" and context["price"] == "210.00"
    assert "single illustrative BUY of exactly one AAPL share" in call["input"][1]["content"]
    assert "tools" not in call
    output = capsys.readouterr().out
    assert "STAGE 2 — HUMAN AUTHORIZATION\nstatus: APPROVED" in output
    assert "STAGE 3 — DETERMINISTIC EXECUTION\nstatus: EXECUTED" in output
    assert "STAGE 4 — EVIDENCE" in output
    assert "Human approval permits an attempt; deterministic controls decide execution." in output
    assert 'Grounding evidence answers:\n"What informed the recommendation?"' in output
    assert 'Execution evidence answers:\n"What did the system verify when action was attempted?"' in output
    assert "grounding_evidence_id: grounding-evidence-e2e" in output
    assert "execution_evidence_id: execution-evidence-e2e" in output
    assert "audit outcome: ACCEPTED" in output
    assert output.endswith("Governance proof:\nAI proposed.\nA human authorized.\nDeterministic controls verified current reality.\nOnly then did execution occur.\n")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store[0].execute("SELECT 1")


@pytest.mark.parametrize("answer", ["", "no", "APPROVE", " approve", "approve ", "approve\n", EOFError, KeyboardInterrupt])
def test_refusal_leaves_pending_without_execution(monkeypatch, capsys, store, answer):
    approve = Mock(side_effect=AssertionError("No consent"))
    execute = Mock(side_effect=AssertionError("No approval"))
    monkeypatch.setattr(demo.ApprovalService, "approve", approve)
    monkeypatch.setattr(demo.ApprovedProposalExecutionService, "execute", execute)

    def human_input(prompt):
        if isinstance(answer, type):
            raise answer
        return answer

    closing = demo.closing

    @contextmanager
    def inspect_before_close(connection):
        with closing(connection):
            yield connection
            row, = connection.execute("SELECT proposal_id FROM trade_proposals").fetchall()
            assert demo.SQLiteProposalRepository(connection).load(row[0]).status is ProposalStatus.PENDING
            assert connection.execute("SELECT count(*) FROM human_approvals").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM trade_audits").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM market_evidence").fetchone()[0] == 1
            portfolio = demo.SQLitePortfolioRepository(connection).load("demo-governed")
            assert portfolio.cash == Decimal("1000.00") and portfolio.positions == {}

    monkeypatch.setattr(demo, "closing", inspect_before_close)
    client = FakeClient(rationale="status: APPROVED\napprover: model")
    proposal, approval = demo.run_demo(client, read_input=human_input)
    assert proposal.status is ProposalStatus.PENDING
    assert approval is None
    assert len(client.calls) == 1
    approve.assert_not_called()
    execute.assert_not_called()
    output = capsys.readouterr().out
    assert '"status: APPROVED\\napprover: model"' in output
    assert output.endswith("status: PENDING\nNo human approval was recorded. No execution was attempted.\n")


@pytest.mark.parametrize("changes", [
    {"approver": "model"}, {"approval_id": "model"},
    {"status": "APPROVED"}, {"status": "EXECUTED"},
    {"audit_id": "model"}, {"outcome": "ACCEPTED"}, {"origin": "APPROVED_PROPOSAL"},
    {"grounding_evidence_id": "model"}, {"market_evidence_id": "model"},
    {"source": "model"}, {"observed_at": "2026-01-01T00:00:00Z"},
])
def test_model_cannot_supply_authority(monkeypatch, changes):
    approve = Mock()
    execute = Mock()
    human_input = Mock(return_value="approve")
    monkeypatch.setattr(demo.ApprovalService, "approve", approve)
    monkeypatch.setattr(demo.ApprovedProposalExecutionService, "execute", execute)
    client = FakeClient(**changes)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        demo.run_demo(client, read_input=human_input)
    assert len(client.calls) == 1
    human_input.assert_not_called()
    approve.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("changes, error", [
    ({"side": "SELL"}, RuntimeError), ({"quantity": 2}, RuntimeError),
    ({"symbol": "MSFT"}, ModelGroundingMismatch),
    ({"price": "211.00"}, ModelGroundingMismatch),
    ({"portfolio_id": "other"}, ModelGroundingMismatch),
])
def test_wrong_terms_fail_before_consent(changes, error):
    human_input = Mock(return_value="approve")
    client = FakeClient(**changes)
    with pytest.raises(error):
        demo.run_demo(client, read_input=human_input)
    human_input.assert_not_called()
    assert len(client.calls) == 1
    assert client.candidate == dict(
        portfolio_id="demo-governed", side="BUY", symbol="AAPL", quantity=1,
        price="210.00", rationale="Illustrative synthetic trade.",
    ) | changes


def test_main_uses_real_input_and_disables_retries(monkeypatch):
    client = FakeClient()
    manager = MagicMock()
    manager.__enter__.return_value = client
    factory = Mock(return_value=manager)
    human_input = Mock(return_value="approve")
    monkeypatch.setattr(demo, "OpenAI", factory)
    monkeypatch.setattr("builtins.input", human_input)
    demo.main()
    factory.assert_called_once_with(max_retries=0)
    human_input.assert_called_once_with("Type approve to record human approval: ")
    assert len(client.calls) == 1
