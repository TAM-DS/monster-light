"""Append-only SQLite trade evidence, with exact decimal text."""

import sqlite3
from dataclasses import astuple

from monster_light.application.audit import AuditOrigin, AuditOutcome, TradeAudit
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint


class SQLiteAuditRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        # Rebuild the legacy DIRECT-only CHECK atomically, including protections.
        # Savepoints preserve ownership of any caller transaction.
        with sqlite_savepoint(self.connection):
            columns = self.connection.execute("PRAGMA table_info(trade_audits)").fetchall()
            legacy = bool(columns) and "proposal_id" not in {row[1] for row in columns}
            if legacy:
                self.connection.execute("ALTER TABLE trade_audits RENAME TO trade_audits_legacy")
            self._create_table()
            if legacy:
                self.connection.execute(
                    "INSERT INTO trade_audits SELECT *, NULL, NULL FROM trade_audits_legacy"
                )
                self.connection.execute("DROP TABLE trade_audits_legacy")
            self._create_triggers()

    def _create_table(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS trade_audits (
                audit_id TEXT PRIMARY KEY NOT NULL,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL CHECK (origin IN ('DIRECT', 'APPROVED_PROPOSAL')),
                portfolio_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                symbol TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price TEXT NOT NULL CHECK (typeof(price) = 'text'),
                outcome TEXT NOT NULL CHECK (outcome IN ('ACCEPTED', 'REJECTED')),
                reason_code TEXT NOT NULL,
                reason_message TEXT NOT NULL,
                cash_before TEXT CHECK (cash_before IS NULL OR typeof(cash_before) = 'text'),
                cash_after TEXT CHECK (cash_after IS NULL OR typeof(cash_after) = 'text'),
                quantity_before INTEGER,
                quantity_after INTEGER,
                proposal_id TEXT,
                approval_id TEXT,
                CHECK (
                    (origin = 'DIRECT' AND proposal_id IS NULL AND approval_id IS NULL)
                    OR (origin = 'APPROVED_PROPOSAL'
                        AND proposal_id IS NOT NULL AND length(trim(proposal_id)) > 0
                        AND approval_id IS NOT NULL AND length(trim(approval_id)) > 0)
                )
            )
        """)

    def _create_triggers(self) -> None:
        for operation in ("UPDATE", "DELETE"):
            self.connection.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trade_audits_no_{operation.lower()}
                BEFORE {operation} ON trade_audits
                BEGIN SELECT RAISE(ABORT, 'Trade audits are append-only'); END
            """)
        # REPLACE can bypass DELETE triggers when recursive_triggers is off.
        self.connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trade_audits_no_replace
            BEFORE INSERT ON trade_audits
            WHEN EXISTS (SELECT 1 FROM trade_audits WHERE audit_id = NEW.audit_id)
            BEGIN SELECT RAISE(ABORT, 'Trade audits are append-only'); END
        """)

    def append(self, audit: TradeAudit) -> None:
        """Insert only; transaction ownership belongs to the application service."""
        values = tuple(
            value.value if isinstance(value, (AuditOrigin, AuditOutcome)) else value
            for value in astuple(audit)
        )
        self.connection.execute(
            "INSERT INTO trade_audits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
