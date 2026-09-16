"""Append-only SQLite trade evidence, with exact decimal text."""

import sqlite3
from dataclasses import astuple

from monster_light.application.audit import AuditOrigin, AuditOutcome, TradeAudit


class SQLiteAuditRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS trade_audits (
                audit_id TEXT PRIMARY KEY NOT NULL,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL CHECK (origin = 'DIRECT'),
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
                quantity_after INTEGER
            )
        """)
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
            "INSERT INTO trade_audits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
