"""AI supplies candidate terms, never authority to trade."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService, HumanApproval
from monster_light.application.audit import TradeAudit
from monster_light.application.market_evidence import MarketEvidence
from monster_light.application.proposal import ProposalOrigin, ProposalStatus, ProposalNotApproved
from monster_light.application.proposal_execution import ApprovedProposalExecutionService
from monster_light.application.trade_service import TradeService, TradeSide
from monster_light.domain.portfolio import Portfolio
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import SQLitePortfolioRepository


def terms(**changes):
    return dict(portfolio_id="one", side=TradeSide.SELL, symbol=" aApL \t",
                quantity=500, price=Decimal("123.4567890123456789012345678900"),
                rationale=" AI suggests selling; unverified. \n",
                grounding_evidence_id=" exact-grounding-id \t") | changes


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "ai.sqlite"
    connection = sqlite3.connect(path)
    repository = SQLiteProposalRepository(connection)
    repository.initialize_schema()
    yield path, connection, repository
    connection.close()


def test_ai_pending_exact_terms_and_reopen(database):
    path, connection, repository = database
    before = datetime.now(timezone.utc)
    proposal = AIProposalService(repository).create(**terms())
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.origin is ProposalOrigin.AI
    assert before <= proposal.created_at <= datetime.now(timezone.utc)
    assert proposal.created_at.utcoffset() == timedelta(0)
    assert UUID(proposal.proposal_id).version == 4
    for name, value in terms().items():
        assert getattr(proposal, name) == value
    assert proposal.price.as_tuple() == terms()["price"].as_tuple()
    another = AIProposalService(repository).create(**terms())
    assert another.proposal_id != proposal.proposal_id
    connection.close()
    with sqlite3.connect(path) as reopened:
        loaded = SQLiteProposalRepository(reopened).load(proposal.proposal_id)
        assert loaded == proposal
        assert loaded.origin is ProposalOrigin.AI
        assert loaded.price.as_tuple() == proposal.price.as_tuple()


@pytest.mark.parametrize("field,value", [
    *[(field, value) for field in ("portfolio_id", "symbol", "rationale", "grounding_evidence_id")
      for value in ("", " \t", None, 7)],
    *[("quantity", value) for value in (0, -1, True, False, 1.5, "1", None)],
    *[("price", value) for value in (
        Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("sNaN"),
        Decimal("Infinity"), Decimal("-Infinity"), 1.5, 1, "1", None)],
    *[("side", value) for value in ("BUY", "SELL", None, 1)],
])
def test_invalid_structure_is_not_persisted(database, field, value):
    _, connection, repository = database
    with pytest.raises(ValueError):
        AIProposalService(repository).create(**terms(**{field: value}))
    assert connection.execute("SELECT count(*) FROM trade_proposals").fetchone()[0] == 0


def test_banana_is_not_a_trade_side():
    with sqlite3.connect(":memory:") as connection:
        repository = SQLiteProposalRepository(connection)
        repository.initialize_schema()
        with pytest.raises(ValueError, match="side"):
            AIProposalService(repository).create(**terms(side="BANANA"))
        assert connection.execute("SELECT count(*) FROM trade_proposals").fetchone()[0] == 0


@pytest.mark.parametrize("side", list(TradeSide))
def test_creation_cannot_cross_authority_boundary(database, monkeypatch, side):
    _, connection, repository = database
    portfolios = SQLitePortfolioRepository(connection)
    portfolios.initialize_schema()
    portfolios.save("one", Portfolio.restore(Decimal("0"), {"AAPL": 2}))
    SQLiteApprovalRepository(connection).initialize_schema()
    SQLiteAuditRepository(connection).initialize_schema()
    SQLiteMarketEvidenceRepository(connection).initialize_schema()
    before = list(connection.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("AI proposal creation crossed the trust boundary")

    # Deny SQL access as well as service calls, including reads of state/evidence.
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
            proposal = AIProposalService(repository).create(**terms(side=side, symbol="AAPL"))
            AIProposalService(repository).create(**terms(portfolio_id="nonexistent", side=side))
        finally:
            connection.set_authorizer(None)
    assert proposal.status is ProposalStatus.PENDING
    assert portfolios.load("one").quantity_for("AAPL") == 2
    assert portfolios.load("one").cash == Decimal("0")
    # Only proposal inserts may have changed the database.
    after = [line for line in connection.iterdump()
             if not line.startswith('INSERT INTO "trade_proposals"')]
    assert after == before


def test_ai_origin_does_not_authorize_execution(database):
    _, _, repository = database
    proposal = AIProposalService(repository).create(**terms())
    execution = ApprovedProposalExecutionService(
        repository, maximum_age=timedelta(minutes=5),
        clock=lambda: datetime.now(timezone.utc),
    )
    with pytest.raises(ProposalNotApproved):
        execution.execute(proposal.proposal_id, "missing-evidence")
    assert repository.load(proposal.proposal_id) == proposal


@pytest.mark.parametrize("field,value", [
    ("proposal_id", "changed"), ("created_at", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ("origin", ProposalOrigin.MANUAL), ("portfolio_id", "other"),
    ("side", TradeSide.BUY), ("symbol", "MSFT"), ("quantity", 1),
    ("price", Decimal("1")), ("rationale", "changed"),
    ("grounding_evidence_id", "replacement"),
])
def test_ai_terms_are_immutable(database, field, value):
    _, connection, repository = database
    proposal = AIProposalService(repository).create(**terms())
    with pytest.raises(FrozenInstanceError):
        setattr(proposal, field, value)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE trade_proposals SET {field} = {field}")
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(replace(proposal, rationale="Changed terms"))
    assert repository.load(proposal.proposal_id) == proposal


def test_grounding_argument_is_required(database):
    _, connection, repository = database
    inputs = terms()
    del inputs['grounding_evidence_id']
    with pytest.raises(TypeError, match='grounding_evidence_id'):
        AIProposalService(repository).create(**inputs)
    assert connection.execute('SELECT count(*) FROM trade_proposals').fetchone()[0] == 0
