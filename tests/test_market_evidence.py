"""Immutable, caller-supplied observations and deterministic freshness."""

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from monster_light.application.market_evidence import (
    MarketEvidence, MarketEvidenceNotFound, InvalidMarketEvidenceTime, StaleMarketEvidence,
)
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observation(**changes):
    return MarketEvidence(**(dict(evidence_id='quote-1', source='Caller supplied fixture',
                                 symbol='AAPL', price=Decimal('10.250000'),
                                 observed_at=NOW, retrieved_at=NOW) | changes))


def test_durable_exact_round_trip(tmp_path):
    path = tmp_path / 'evidence.sqlite'
    evidence = observation()
    with sqlite3.connect(path) as connection:
        repository = SQLiteMarketEvidenceRepository(connection)
        repository.initialize_schema()
        repository.append(evidence)
        assert not connection.in_transaction
        assert repository.load(evidence.evidence_id) == evidence
        assert connection.execute('SELECT price, typeof(price), source FROM market_evidence').fetchone() == (
            '10.250000', 'text', evidence.source)
    with sqlite3.connect(path) as reopened:
        loaded = SQLiteMarketEvidenceRepository(reopened).load(evidence.evidence_id)
        assert loaded == evidence
        assert loaded.price.as_tuple() == evidence.price.as_tuple()
    with pytest.raises(FrozenInstanceError):
        evidence.source = 'changed'


@pytest.mark.parametrize('price', [0.1, 1, Decimal('0'), Decimal('-1'), Decimal('NaN'),
                                   Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity')])
def test_invalid_price(price):
    with pytest.raises(ValueError, match='Decimal'):
        observation(price=price)


@pytest.mark.parametrize('field', ['evidence_id', 'source', 'symbol'])
@pytest.mark.parametrize('value', ['', ' \t', None])
def test_nonempty_fields(field, value):
    with pytest.raises(ValueError):
        observation(**{field: value})


@pytest.mark.parametrize('field', ['observed_at', 'retrieved_at'])
@pytest.mark.parametrize('value', [NOW.replace(tzinfo=None),
                                   NOW.astimezone(timezone(timedelta(hours=1))), None])
def test_utc_required(field, value):
    with pytest.raises(InvalidMarketEvidenceTime):
        observation(**{field: value})


def test_observation_cannot_follow_retrieval():
    with pytest.raises(InvalidMarketEvidenceTime):
        observation(observed_at=NOW + timedelta(microseconds=1))


@pytest.mark.parametrize('statement', [
    "UPDATE market_evidence SET source = 'changed'", 'DELETE FROM market_evidence',
    'INSERT OR REPLACE INTO market_evidence SELECT * FROM market_evidence',
    'REPLACE INTO market_evidence SELECT * FROM market_evidence',
    'INSERT INTO market_evidence SELECT * FROM market_evidence',
])
def test_append_only_and_unknown_id(statement):
    with sqlite3.connect(':memory:') as connection:
        repository = SQLiteMarketEvidenceRepository(connection)
        repository.initialize_schema()
        evidence = observation()
        repository.append(evidence)
        with pytest.raises(MarketEvidenceNotFound):
            repository.load('unknown')
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            repository.append(replace(evidence, source='replacement'))
        assert repository.load(evidence.evidence_id) == evidence


@pytest.mark.parametrize('age,window,stale', [(0, 0, False), (299, 300, False),
    (300, 300, False), (301, 300, True), (1800, 300, True), (1800, 1800, False)])
def test_freshness_uses_observation_and_configured_window(age, window, stale):
    evidence = observation(observed_at=NOW - timedelta(seconds=age))
    if stale:
        with pytest.raises(StaleMarketEvidence):
            evidence.validate_freshness(now=NOW, maximum_age=timedelta(seconds=window))
    else:
        evidence.validate_freshness(now=NOW, maximum_age=timedelta(seconds=window))


@pytest.mark.parametrize('now', [NOW - timedelta(microseconds=1), NOW.replace(tzinfo=None)])
def test_invalid_evaluation_time(now):
    with pytest.raises(InvalidMarketEvidenceTime):
        observation().validate_freshness(now=now, maximum_age=timedelta(minutes=5))


@pytest.mark.parametrize('window', [None, 300, timedelta(microseconds=-1)])
def test_invalid_window(window):
    with pytest.raises(ValueError):
        observation().validate_freshness(now=NOW, maximum_age=window)
