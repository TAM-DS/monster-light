"""Exercise the offline demo through the real approval and execution services."""

import importlib
import socket
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from monster_light.application.proposal import ProposalOrigin, ProposalStatus
from monster_light.application.proposal_execution import ApprovedProposalExecutionService
from monster_light.application.trade_service import TradeService
from monster_light.demo import deterministic_execution as demo
from monster_light.domain.portfolio import InsufficientCash
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository


@pytest.fixture
def observed_demo(monkeypatch, capsys):
    connect = sqlite3.connect
    execute = ApprovedProposalExecutionService.execute
    trade = TradeService.execute
    connections = []
    attempts = []
    trades = []
    rejections = []
    active = []

    def offline(*args, **kwargs):
        pytest.fail("The execution demo must stay offline")

    def capture_connection(database, *args, **kwargs):
        assert database == ":memory:"
        connection = connect(database, *args, **kwargs)
        connections.append(connection)
        return connection

    def capture_execution(self, proposal_id, market_evidence_id):
        connection, = connections
        proposal = SQLiteProposalRepository(connection).load(proposal_id)
        approval = SQLiteApprovalRepository(connection).load(proposal_id)
        assert proposal.status is ProposalStatus.APPROVED
        assert proposal.origin is ProposalOrigin.AI
        assert approval is not None
        assert approval.proposal_id == proposal_id
        evidence = SQLiteMarketEvidenceRepository(connection).load(market_evidence_id)
        assert evidence.source == "synthetic-execution-demo"
        assert evidence.symbol == proposal.symbol
        assert evidence.price == proposal.price
        assert evidence.observed_at.utcoffset().total_seconds() == 0
        assert evidence.observed_at <= datetime.now(timezone.utc)
        assert evidence.evidence_id != proposal.grounding_evidence_id
        attempts.append((proposal, approval, evidence))
        active.append(proposal_id)
        try:
            return execute(self, proposal_id, market_evidence_id)
        except InsufficientCash as error:
            rejections.append(error)
            raise
        finally:
            active.pop()

    def capture_trade(self, request, **kwargs):
        proposal_id, = active
        assert kwargs["audit_context"].proposal_id == proposal_id
        trades.append((request, kwargs["audit_context"]))
        return trade(self, request, **kwargs)

    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    # A model SDK import would fail even if it never reached the network.
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setitem(
        sys.modules, "monster_light.application.model_proposal_adapter", None,
    )
    importlib.reload(demo)
    monkeypatch.setattr(sqlite3, "connect", capture_connection)
    monkeypatch.setattr(ApprovedProposalExecutionService, "execute", capture_execution)
    monkeypatch.setattr(TradeService, "execute", capture_trade)
    success, failure = demo.run_demo()
    assert len(connections) == 1
    assert len(attempts) == len(trades) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    return success, failure, attempts, trades, rejections


def assert_provenance(result, attempt, trade, suffix):
    proposal, approval, evidence = attempt
    request, context = trade
    assert result.before == proposal
    assert result.approval == approval
    assert request.portfolio_id == proposal.portfolio_id
    assert request.symbol == "AAPL"
    assert request.quantity == 1
    assert request.price == Decimal("210.00")
    assert result.after.grounding_evidence_id == f"grounding-evidence-{suffix}"
    assert result.before.grounding_evidence_id == result.after.grounding_evidence_id
    assert result.audit["origin"] == "APPROVED_PROPOSAL"
    assert result.audit["proposal_id"] == context.proposal_id == proposal.proposal_id
    assert result.audit["approval_id"] == context.approval_id == approval.approval_id
    assert result.audit["market_evidence_id"] == context.market_evidence_id == evidence.evidence_id
    assert evidence.evidence_id == f"execution-evidence-{suffix}"


def test_success_uses_production_execution_and_preserves_provenance(observed_demo):
    success, _, attempts, trades, _ = observed_demo
    assert_provenance(success, attempts[0], trades[0], "001")
    assert success.error is None
    assert success.after == replace(success.before, status=ProposalStatus.EXECUTED)
    assert success.portfolio_before.cash == Decimal("1000.00")
    assert success.portfolio_before.positions == {}
    assert success.portfolio_after.cash == Decimal("790.00")
    assert success.portfolio_after.positions == {"AAPL": 1}
    assert success.audit["outcome"] == "ACCEPTED"
    assert success.audit["cash_before"] == "1000.00"
    assert success.audit["cash_after"] == "790.00"
    assert success.audit["quantity_before"] == 0
    assert success.audit["quantity_after"] == 1


def test_approval_does_not_override_insufficient_cash(observed_demo):
    _, failure, attempts, trades, rejections = observed_demo
    assert_provenance(failure, attempts[1], trades[1], "002")
    error, = rejections
    assert type(error) is InsufficientCash
    assert failure.error is error
    assert failure.after == failure.before
    assert failure.after.status is ProposalStatus.APPROVED
    assert failure.portfolio_before.cash == failure.portfolio_after.cash == Decimal("100.00")
    assert failure.portfolio_before.positions == failure.portfolio_after.positions == {}
    assert failure.audit["outcome"] == "REJECTED"
    assert failure.audit["reason_code"] == "InsufficientCash"
    assert failure.audit["reason_message"] == str(error)
    assert failure.audit["cash_before"] == failure.audit["cash_after"] == "100.00"
    assert failure.audit["quantity_before"] == failure.audit["quantity_after"] == 0


def test_output_explains_both_outcomes_and_authority_boundary(observed_demo, capsys):
    output = capsys.readouterr().out
    assert "Case 1: APPROVED -> deterministic validation passes -> EXECUTED" in output
    assert "Case 2: APPROVED -> deterministic validation fails -> remains APPROVED" in output
    assert "human approvals are fixtures recorded by ApprovalService" in output
    assert "grounding_evidence_id: grounding-evidence-001" in output
    assert "execution_evidence_id: execution-evidence-001" in output
    assert "audit outcome: ACCEPTED" in output
    assert "audit outcome: REJECTED" in output
    assert output.endswith(
        "Authority boundary: human approval permitted an execution attempt. "
        "Deterministic validation still controlled the outcome.\n"
    )
