"""Tests for src/execution/instruments.py — the Kite tradingsymbol -> instrument_token
disk cache. No real kiteconnect package or credentials needed."""
from __future__ import annotations

import time

from src.execution import instruments


class _StubKite:
    def __init__(self, rows=None, raise_on_instruments=False):
        self._rows = rows or [
            {"tradingsymbol": "EMCURE", "instrument_token": 111},
            {"tradingsymbol": "TATAMOTORS", "instrument_token": 222},
        ]
        self._raise_on_instruments = raise_on_instruments

    def instruments(self, exchange):
        if self._raise_on_instruments:
            raise ConnectionError("kite down")
        return self._rows


def _cache_file(tmp_path, monkeypatch):
    path = tmp_path / "instruments.json"
    monkeypatch.setenv("KITE_INSTRUMENTS_CACHE", str(path))
    return path


def test_refresh_instrument_map_builds_and_caches(tmp_path, monkeypatch):
    cache = _cache_file(tmp_path, monkeypatch)
    kite = _StubKite()
    mapping = instruments.refresh_instrument_map(kite)
    assert mapping == {"EMCURE": 111, "TATAMOTORS": 222}
    assert cache.exists()


def test_instrument_token_resolves_and_caches(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    kite = _StubKite()
    assert instruments.instrument_token(kite, "EMCURE") == 111
    assert instruments.instrument_token(kite, "EMCURE.NS") == 111  # strips .NS
    assert instruments.instrument_token(kite, "emcure") == 111     # case-insensitive


def test_instrument_token_uses_cache_without_refetching(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    calls = []
    kite = _StubKite()
    original_instruments = kite.instruments
    kite.instruments = lambda exchange: (calls.append(1) or original_instruments(exchange))

    instruments.instrument_token(kite, "EMCURE")
    instruments.instrument_token(kite, "EMCURE")  # second call should hit the cache
    assert len(calls) == 1


def test_instrument_token_refreshes_when_cache_stale(tmp_path, monkeypatch):
    cache = _cache_file(tmp_path, monkeypatch)
    kite = _StubKite()
    instruments.instrument_token(kite, "EMCURE")
    # Backdate the cache beyond the 24h freshness window.
    import json
    data = json.loads(cache.read_text())
    data["fetched_at"] = time.time() - 25 * 3600
    cache.write_text(json.dumps(data))

    calls = []
    original_instruments = kite.instruments
    kite.instruments = lambda exchange: (calls.append(1) or original_instruments(exchange))
    instruments.instrument_token(kite, "EMCURE")
    assert len(calls) == 1  # refreshed once, because cache was stale


def test_instrument_token_refreshes_on_symbol_not_in_cached_map(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    kite = _StubKite(rows=[{"tradingsymbol": "EMCURE", "instrument_token": 111}])
    assert instruments.instrument_token(kite, "TATAMOTORS") is None  # not present after refresh too

    kite._rows.append({"tradingsymbol": "TATAMOTORS", "instrument_token": 222})
    assert instruments.instrument_token(kite, "TATAMOTORS") == 222  # refetched, now found


def test_instrument_token_returns_none_on_refresh_failure(tmp_path, monkeypatch):
    _cache_file(tmp_path, monkeypatch)
    kite = _StubKite(raise_on_instruments=True)
    assert instruments.instrument_token(kite, "EMCURE") is None
