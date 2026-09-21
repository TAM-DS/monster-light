"""Near-real-time market observations adapted into Monster Light evidence."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from monster_light.application.market_evidence import MarketEvidence


class MarketDataUnavailable(RuntimeError):
    """The external quote source could not provide a usable observation."""


class YFinanceQuoteProvider:
    """Fetch one-minute Yahoo Finance data without granting trading authority.

    The provider is deliberately an infrastructure adapter. It may observe the
    market and construct evidence, but it cannot approve proposals or mutate a
    portfolio.
    """

    def __init__(
        self,
        *,
        ticker_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ticker_factory = ticker_factory or self._default_ticker_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _default_ticker_factory(symbol: str) -> Any:
        try:
            import yfinance as yf
        except ImportError as error:
            raise MarketDataUnavailable(
                "yfinance is required for live market observations"
            ) from error
        return yf.Ticker(symbol)

    def fetch(self, symbol: str) -> MarketEvidence:
        normalized = self._normalize_symbol(symbol)
        retrieved_at = self._clock()
        if (
            not isinstance(retrieved_at, datetime)
            or retrieved_at.utcoffset() != timedelta(0)
        ):
            raise MarketDataUnavailable("Quote retrieval clock must return UTC")

        try:
            history = self._ticker_factory(normalized).history(
                period="1d",
                interval="1m",
                prepost=False,
                actions=False,
                auto_adjust=False,
                raise_errors=True,
            )
        except Exception as error:
            raise MarketDataUnavailable(
                f"Could not retrieve a quote for {normalized}"
            ) from error

        if getattr(history, "empty", True):
            raise MarketDataUnavailable(f"No intraday quote available for {normalized}")

        try:
            close = history["Close"].iloc[-1]
            raw_observed_at = history.index[-1]
            if hasattr(raw_observed_at, "to_pydatetime"):
                raw_observed_at = raw_observed_at.to_pydatetime()
            if (
                not isinstance(raw_observed_at, datetime)
                or raw_observed_at.utcoffset() is None
            ):
                raise ValueError("Observation timestamp must be timezone-aware")
            observed_at = raw_observed_at.astimezone(timezone.utc)
            price = Decimal(str(close))
        except (IndexError, KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise MarketDataUnavailable(
                f"Quote payload for {normalized} was not usable"
            ) from error

        if not price.is_finite() or price <= 0:
            raise MarketDataUnavailable(
                f"Quote price for {normalized} must be positive and finite"
            )

        try:
            return MarketEvidence(
                evidence_id=f"yf-{uuid4()}",
                source="Yahoo Finance via yfinance",
                symbol=normalized,
                price=price,
                observed_at=observed_at,
                retrieved_at=retrieved_at,
            )
        except ValueError as error:
            raise MarketDataUnavailable(
                f"Quote timing for {normalized} was not coherent"
            ) from error

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        return symbol.strip().upper()
