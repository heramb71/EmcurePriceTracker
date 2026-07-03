"""Tests for crash-safe, lock-guarded JSON persistence."""
from __future__ import annotations

import json
import os
import threading

from src.shared.atomic_json import locked, read_json, write_json


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"a": 1, "b": [1, 2, 3]})
    assert read_json(path, {}) == {"a": 1, "b": [1, 2, 3]}


def test_read_missing_returns_default(tmp_path):
    assert read_json(tmp_path / "nope.json", {"fallback": True}) == {"fallback": True}


def test_read_corrupt_returns_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not valid json")
    assert read_json(path, {"safe": 1}) == {"safe": 1}


def test_write_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"x": 1})
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []


def test_overwrite_is_atomic_never_truncates(tmp_path):
    """A reader interleaved with many rewrites must always see complete JSON.

    Emulates the real race: one thread rewrites the file in a loop while another
    reads it. With os.replace the reader can only ever observe the old or the
    new file — never a half-written one — so json.load must never raise.
    """
    path = tmp_path / "state.json"
    write_json(path, {"n": 0})
    stop = threading.Event()
    errors: list[Exception] = []

    def writer():
        for n in range(500):
            write_json(path, {"n": n, "pad": "x" * 500})
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                with open(path) as f:
                    json.load(f)  # must always parse
            except json.JSONDecodeError as exc:  # pragma: no cover - the bug we prevent
                errors.append(exc)
            except FileNotFoundError:
                pass

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_r.start()
    t_w.start()
    t_w.join()
    t_r.join()
    assert errors == []


def test_default_serializer_applied(tmp_path):
    from datetime import datetime

    path = tmp_path / "state.json"
    write_json(path, {"when": datetime(2026, 7, 3, 9, 15)}, default=str)
    assert read_json(path, {})["when"].startswith("2026-07-03")


def test_locked_serializes_read_modify_write(tmp_path):
    """Concurrent increment-under-lock must not lose updates."""
    path = tmp_path / "counter.json"
    write_json(path, {"count": 0})

    def bump():
        for _ in range(200):
            with locked(path):
                state = read_json(path, {"count": 0})
                state["count"] += 1
                write_json(path, state)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 threads * 200 increments, none lost because each RMW held the lock.
    assert read_json(path, {})["count"] == 800
