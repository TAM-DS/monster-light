from datetime import datetime, timezone
from decimal import Decimal

import pytest

from monster_light.infrastructure.yfinance_quote_provider import (
    MarketDataUnavailable,
    YFinanceQuoteProvider,
)


class _Series:
    def __init__(self, value):
        self.iloc = self
        self._value = value

    def __getitem__(self, index):
        if index != -1:
            raise IndexError(index)
        return self._value


class _History:
    def __init__(self, *, price="210.25", observed_at=None, empty=False):
        self.empty = empty
        self._price = price
        self.index = [
            observed_at
            or datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
        ]

    def __getitem__(self, key):
        if key != "Close":
            raise KeyError(key)
        return _Series(self._price)


class _Ticker:
    def __init__(self, history):
        self._history = history
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        return self._history


def test_fetch_returns_governance_evidence_without_authority():
    ticker = _Ticker(_History())
    provider = YFinanceQuoteProvider(
        ticker_factory=lambda symbol: ticker,
        clock=lambda: datetime(2026, 9, 21, 15, 1, tzinfo=timezone.utc),
    )

    evidence = provider.fetch(" aapl ")

    assert evidence.symbol == "AAPL"
    assert evidence.price == Decimal("210.25")
    assert evidence.source == "Yahoo Finance via yfinance"
    assert evidence.evidence_id.startswith("yf-")
    assert evidence.observed_at == datetime(
        2026, 9, 21, 15, 0, tzinfo=timezone.utc
    )
    assert evidence.retrieved_at == datetime(
        2026, 9, 21, 15, 1, tzinfo=timezone.utc
    )
    assert ticker.calls == [{
        "period": "1d",
        "interval": "1m",
        "prepost": False,
        "actions": False,
        "auto_adjust": False,
        "raise_errors": True,
    }]


def test_fetch_rejects_empty_history():
    provider = YFinanceQuoteProvider(
        ticker_factory=lambda symbol: _Ticker(_History(empty=True)),
        clock=lambda: datetime(2026, 9, 21, 15, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(MarketDataUnavailable, match="No intraday quote"):
        provider.fetch("AAPL")


def test_fetch_rejects_unusable_price():
    provider = YFinanceQuoteProvider(
        ticker_factory=lambda symbol: _Ticker(_History(price="not-a-price")),
        clock=lambda: datetime(2026, 9, 21, 15, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(MarketDataUnavailable, match="not usable"):
        provider.fetch("AAPL")


def test_fetch_requires_nonempty_symbol():
    provider = YFinanceQuoteProvider(
        ticker_factory=lambda symbol: _Ticker(_History()),
    )

    with pytest.raises(ValueError, match="non-empty"):
        provider.fetch("   ")
