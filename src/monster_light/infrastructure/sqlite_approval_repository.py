"""Append-only human approval evidence, bound by immutable proposal identifier."""

import sqlite3
from datetime import datetime

from monster_light.application.approval import HumanApproval


class SQLiteApprovalRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS human_approvals (
                approval_id TEXT PRIMARY KEY NOT NULL,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(proposal_id),
                approved_at TEXT NOT NULL,
                approver TEXT NOT NULL CHECK (length(trim(approver)) > 0)
            )
        """)
        for operation in ("UPDATE", "DELETE"):
            self.connection.execute(f"""
                CREATE TRIGGER IF NOT EXISTS human_approvals_no_{operation.lower()}
                BEFORE {operation} ON human_approvals
                BEGIN SELECT RAISE(ABORT, 'Human approvals are append-only'); END
            """)
        # REPLACE may bypass DELETE triggers with recursive_triggers disabled.
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS human_approvals_no_replace
            BEFORE INSERT ON human_approvals
            WHEN EXISTS (SELECT 1 FROM human_approvals
                         WHERE approval_id = NEW.approval_id OR proposal_id = NEW.proposal_id)
            BEGIN SELECT RAISE(ABORT, 'Human approvals are append-only'); END
        """)
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS human_approvals_require_approved
            BEFORE INSERT ON human_approvals
            WHEN NOT EXISTS (SELECT 1 FROM trade_proposals
                             WHERE proposal_id = NEW.proposal_id AND status = 'APPROVED')
            BEGIN SELECT RAISE(ABORT, 'Approval requires an approved proposal'); END
        """)

    def append(self, approval: HumanApproval) -> None:
        """Insert only; ApprovalService owns the encompassing savepoint."""
        if not isinstance(approval, HumanApproval):
            raise ValueError("approval must be a HumanApproval")
        self.connection.execute(
            "INSERT INTO human_approvals VALUES (?, ?, ?, ?)",
            (approval.approval_id, approval.proposal_id,
             approval.approved_at.isoformat(), approval.approver),
        )

    def load(self, proposal_id: str) -> HumanApproval | None:
        row = self.connection.execute(
            "SELECT approval_id, proposal_id, approved_at, approver "
            "FROM human_approvals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        return HumanApproval(approval_id=row[0], proposal_id=row[1],
                             approved_at=datetime.fromisoformat(row[2]), approver=row[3])
