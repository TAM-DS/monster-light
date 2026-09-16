"""Durable candidate trades; no access to authoritative portfolio state."""

import sqlite3
from datetime import datetime
from decimal import Decimal

from monster_light.application.proposal import (
    ProposalAlreadyApproved, ProposalAlreadyRejected, ProposalNotFound, ProposalOrigin, ProposalStatus,
    TradeProposal,
)
from monster_light.application.trade_service import TradeSide
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint


class SQLiteProposalRepository:
    """Callers own connections and any outer transaction.

    Saves insert new PENDING records only. With no outer transaction, writes
    commit immediately; otherwise the caller controls commit or rollback.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        with sqlite_savepoint(self.connection):
            schema = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trade_proposals'"
            ).fetchone()
            # The original CHECK constraint cannot be expanded with ALTER TABLE.
            legacy = schema is not None and "'APPROVED'" not in schema[0]
            if legacy:
                for trigger in ("immutable_terms", "reject_only", "no_delete", "no_replace"):
                    self.connection.execute(f"DROP TRIGGER IF EXISTS trade_proposals_{trigger}")
                self.connection.execute("ALTER TABLE trade_proposals RENAME TO legacy_trade_proposals")
            self._create_schema()
            if legacy:
                self.connection.execute(
                    "INSERT INTO trade_proposals SELECT * FROM legacy_trade_proposals"
                )
                self.connection.execute("DROP TABLE legacy_trade_proposals")

    def _create_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS trade_proposals (
                proposal_id TEXT PRIMARY KEY NOT NULL,
                created_at TEXT NOT NULL,
                origin TEXT NOT NULL CHECK (origin = 'MANUAL'),
                portfolio_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL
                    CHECK (typeof(quantity) = 'integer' AND quantity > 0),
                price TEXT NOT NULL CHECK (typeof(price) = 'text'),
                rationale TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
                rejection_reason TEXT,
                CHECK ((status IN ('PENDING', 'APPROVED') AND rejection_reason IS NULL) OR
                       (status = 'REJECTED' AND rejection_reason IS NOT NULL
                        AND length(trim(rejection_reason)) > 0))
            )
        """)
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trade_proposals_immutable_terms
            BEFORE UPDATE OF proposal_id, created_at, origin, portfolio_id,
                side, symbol, quantity, price, rationale ON trade_proposals
            BEGIN SELECT RAISE(ABORT, 'Proposal terms are immutable'); END
        """)
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trade_proposals_transition_only
            BEFORE UPDATE ON trade_proposals
            WHEN OLD.status != 'PENDING' OR NEW.status NOT IN ('APPROVED', 'REJECTED')
            BEGIN SELECT RAISE(ABORT, 'Only PENDING proposals may be approved or rejected'); END
        """)
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trade_proposals_no_delete
            BEFORE DELETE ON trade_proposals
            BEGIN SELECT RAISE(ABORT, 'Proposal records are historical'); END
        """)
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trade_proposals_no_replace
            BEFORE INSERT ON trade_proposals
            WHEN EXISTS (SELECT 1 FROM trade_proposals WHERE proposal_id = NEW.proposal_id)
            BEGIN SELECT RAISE(ABORT, 'Proposal records cannot be replaced'); END
        """)

    def save(self, proposal: TradeProposal) -> None:
        """Persist a new candidate; duplicate identifiers fail explicitly."""
        if not isinstance(proposal, TradeProposal):
            raise ValueError("proposal must be a TradeProposal")
        if proposal.status is not ProposalStatus.PENDING:
            raise ValueError("New proposals must be PENDING")
        with sqlite_savepoint(self.connection):
            self.connection.execute("""
                INSERT INTO trade_proposals
                    (proposal_id, created_at, origin, portfolio_id, side, symbol,
                     quantity, price, rationale, status, rejection_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (proposal.proposal_id, proposal.created_at.isoformat(), proposal.origin.value,
                  proposal.portfolio_id, proposal.side.value, proposal.symbol,
                  proposal.quantity, str(proposal.price), proposal.rationale,
                  proposal.status.value, proposal.rejection_reason))

    @staticmethod
    def _restore(row) -> TradeProposal:
        return TradeProposal(
            proposal_id=row[0], created_at=datetime.fromisoformat(row[1]),
            origin=ProposalOrigin(row[2]), portfolio_id=row[3], side=TradeSide(row[4]),
            symbol=row[5], quantity=row[6], price=Decimal(row[7]), rationale=row[8],
            status=ProposalStatus(row[9]), rejection_reason=row[10],
        )

    def load(self, proposal_id: str) -> TradeProposal:
        row = self.connection.execute(
            "SELECT * FROM trade_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ProposalNotFound(proposal_id)
        return self._restore(row)

    def list_for_portfolio(self, portfolio_id: str) -> list[TradeProposal]:
        rows = self.connection.execute("""
            SELECT * FROM trade_proposals WHERE portfolio_id = ?
            ORDER BY created_at, proposal_id
        """, (portfolio_id,)).fetchall()
        return [self._restore(row) for row in rows]

    def reject(self, proposal_id: str, reason: str) -> TradeProposal:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rejection reason must be a non-empty string")
        with sqlite_savepoint(self.connection):
            updated = self.connection.execute("""
                UPDATE trade_proposals SET status = 'REJECTED', rejection_reason = ?
                WHERE proposal_id = ? AND status = 'PENDING'
            """, (reason, proposal_id))
            proposal = self.load(proposal_id)
            if updated.rowcount != 1:
                if proposal.status is ProposalStatus.APPROVED:
                    raise ProposalAlreadyApproved(proposal_id)
                raise ProposalAlreadyRejected(proposal_id)
            return proposal

    def mark_approved(self, proposal_id: str) -> TradeProposal:
        """Used by ApprovalService inside the approval evidence savepoint."""
        with sqlite_savepoint(self.connection):
            updated = self.connection.execute("""
                UPDATE trade_proposals SET status = 'APPROVED'
                WHERE proposal_id = ? AND status = 'PENDING'
            """, (proposal_id,))
            proposal = self.load(proposal_id)
            if updated.rowcount != 1:
                if proposal.status is ProposalStatus.REJECTED:
                    raise ProposalAlreadyRejected(proposal_id)
                raise ProposalAlreadyApproved(proposal_id)
            return proposal
