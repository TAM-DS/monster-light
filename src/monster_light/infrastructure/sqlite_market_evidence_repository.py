"""Durable append-only market observations with exact Decimal text."""

import sqlite3
from datetime import datetime
from decimal import Decimal

from monster_light.application.market_evidence import (
    InvalidMarketEvidenceTime, MarketEvidence, MarketEvidenceNotFound,
)
from monster_light.infrastructure.sqlite_transaction import sqlite_savepoint


class SQLiteMarketEvidenceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialize_schema(self) -> None:
        with sqlite_savepoint(self.connection):
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS market_evidence (
                    evidence_id TEXT PRIMARY KEY NOT NULL,
                    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                    symbol TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
                    price TEXT NOT NULL CHECK (typeof(price) = 'text'),
                    observed_at TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                )
            """)
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS market_evidence_no_{operation.lower()}
                    BEFORE {operation} ON market_evidence
                    BEGIN SELECT RAISE(ABORT, 'Market evidence is append-only'); END
                """)
            # REPLACE bypasses DELETE triggers with recursive_triggers disabled.
            self.connection.execute("""
                CREATE TRIGGER IF NOT EXISTS market_evidence_no_replace
                BEFORE INSERT ON market_evidence
                WHEN EXISTS (SELECT 1 FROM market_evidence WHERE evidence_id = NEW.evidence_id)
                BEGIN SELECT RAISE(ABORT, 'Market evidence is append-only'); END
            """)

    def append(self, evidence: MarketEvidence) -> None:
        """Persist atomically, retaining ownership of any caller transaction."""
        if not isinstance(evidence, MarketEvidence):
            raise ValueError("evidence must be MarketEvidence")
        with sqlite_savepoint(self.connection):
            self.connection.execute(
                "INSERT INTO market_evidence VALUES (?, ?, ?, ?, ?, ?)",
                (evidence.evidence_id, evidence.source, evidence.symbol, str(evidence.price),
                 evidence.observed_at.isoformat(), evidence.retrieved_at.isoformat()),
            )

    def load(self, evidence_id: str) -> MarketEvidence:
        row = self.connection.execute(
            "SELECT evidence_id, source, symbol, price, observed_at, retrieved_at "
            "FROM market_evidence WHERE evidence_id = ?", (evidence_id,),
        ).fetchone()
        if row is None:
            raise MarketEvidenceNotFound(evidence_id)
        try:
            observed_at = datetime.fromisoformat(row[4])
            retrieved_at = datetime.fromisoformat(row[5])
        except (TypeError, ValueError) as error:
            raise InvalidMarketEvidenceTime("Invalid persisted evidence timestamp") from error
        return MarketEvidence(
            evidence_id=row[0], source=row[1], symbol=row[2], price=Decimal(row[3]),
            observed_at=observed_at, retrieved_at=retrieved_at,
        )
