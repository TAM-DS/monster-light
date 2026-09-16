"""Caller-supplied market observations; evidence conveys no trading authority."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


class MarketEvidenceNotFound(LookupError):
    """No persisted evidence has this identifier."""


class StaleMarketEvidence(ValueError):
    """The observation exceeds the configured maximum age."""


class MarketEvidenceMismatch(ValueError):
    """Evidence does not match the immutable proposal terms."""


class InvalidMarketEvidenceTime(ValueError):
    """Evidence or evaluation time is not temporally coherent."""


def require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise InvalidMarketEvidenceTime("Timestamp must be UTC")


@dataclass(frozen=True)
class MarketEvidence:
    evidence_id: str
    source: str
    symbol: str
    price: Decimal
    observed_at: datetime
    retrieved_at: datetime

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (not isinstance(self.price, Decimal) or not self.price.is_finite()
                or self.price <= 0):
            raise ValueError("price must be a positive finite Decimal")
        require_utc(self.observed_at)
        require_utc(self.retrieved_at)
        if self.observed_at > self.retrieved_at:
            raise InvalidMarketEvidenceTime("Observation cannot follow retrieval")

    def validate_freshness(self, *, now: datetime, maximum_age: timedelta) -> None:
        """Accept the inclusive age boundary; future retrievals are incoherent."""
        validate_maximum_age(maximum_age)
        require_utc(now)
        if self.retrieved_at > now:
            raise InvalidMarketEvidenceTime("Retrieval cannot follow execution time")
        if now - self.observed_at > maximum_age:
            raise StaleMarketEvidence(self.evidence_id)


def validate_maximum_age(value: timedelta) -> None:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError("maximum_age must be a nonnegative timedelta")
