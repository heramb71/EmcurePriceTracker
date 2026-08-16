"""Tests for src/execution/historical.py — Kite daily/intraday OHLCV fetches.
No real kiteconnect package or credentials needed."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.execution import historical
from src.execution.broker import KiteBroker


class _StubKite:
    def __init__(self, rows=None, instruments=None, fail_calls=0):
        self._rows = rows if rows is not None else [
            {"date": datetime(2026, 1, 1), "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 1000},
            {"date": datetime(2026, 1, 2), "open": 100.5, "high": 102.0,
             "low": 100.0, "close": 101.5, "volume": 1200},
        ]
        self._instruments = instruments or [
            {"tradingsymbol": "EMCURE", "instrument_token": 111},
        ]
        self._fail_calls = fail_calls  # historical_data() raises this many times first
        self.calls = []

    def instruments(self, exchange):
        return self._instruments

    def historical_data(self, token, from_date, to_date, interval, **kwargs):
        self.calls.append((token, from_date, to_date, interval))
        if self._fail_calls > 0:
            self._fail_calls -= 1
            raise ConnectionError("kite down")
        return self._rows


def _broker(**stub_kwargs) -> KiteBroker:
    b = KiteBroker.__new__(KiteBroker)  # skip __init__ (needs kiteconnect)
    b.kite = _StubKite(**stub_kwargs)
    return b


def _cache_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_INSTRUMENTS_CACHE", str(tmp_path / "instruments.json"))


# ── fetch_daily ──────────────────────────────────────────────────────────────

def test_fetch_daily_returns_expected_shape(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    b = _broker()
    df = historical.fetch_daily(b, "EMCURE", days=10)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["date"].is_monotonic_increasing


def test_fetch_daily_returns_none_for_unknown_symbol(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    b = _broker()
    assert historical.fetch_daily(b, "NOSUCHSYMBOL", days=10) is None


def test_fetch_daily_retries_then_succeeds(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    monkeypatch.setattr(historical.time, "sleep", lambda s: None)  # skip real backoff
    b = _broker(fail_calls=2)  # fails twice, succeeds on the 3rd attempt
    df = historical.fetch_daily(b, "EMCURE", days=10)
    assert df is not None and len(df) == 2


def test_fetch_daily_exhausts_retries_and_returns_none(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    monkeypatch.setattr(historical.time, "sleep", lambda s: None)
    b = _broker(fail_calls=99)  # always fails
    assert historical.fetch_daily(b, "EMCURE", days=10) is None


# ── fetch_intraday ───────────────────────────────────────────────────────────

def test_fetch_intraday_chunks_multi_year_requests(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    b = _broker()
    historical.fetch_intraday(b, "EMCURE", interval="minute", days=200)
    # 200 days at a 60-day chunk cap -> at least 4 chunked calls.
    assert len(b.kite.calls) >= 4
    for _, from_date, to_date, interval in b.kite.calls:
        assert (to_date - from_date) <= timedelta(days=60)
        assert interval == "minute"


def test_fetch_intraday_returns_expected_shape(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    b = _broker()
    df = historical.fetch_intraday(b, "EMCURE", interval="minute", days=30)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["date"].is_monotonic_increasing
    assert not df["date"].duplicated().any()


def test_fetch_intraday_returns_none_for_unknown_symbol(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    b = _broker()
    assert historical.fetch_intraday(b, "NOSUCHSYMBOL", days=30) is None
