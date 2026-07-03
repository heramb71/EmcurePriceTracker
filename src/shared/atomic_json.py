"""
Crash-safe, lock-guarded JSON persistence.

Every runtime state file in this project (trade_state.json, managed_state.json,
strategy_state.json) is a small dict rewritten in full each cycle. A bare
``open(path, "w") + json.dump`` is not safe for that: if the process is killed
mid-write — or if two writers interleave (the bot_server command thread and the
main_headless loop both touch trade_state.json) — the file is left truncated,
and the readers here swallow ``JSONDecodeError`` and fall back to ``{}``. For a
system holding a live position that silently erases the trade.

This module centralises the fix:

- ``write_json`` writes to a temp file in the same directory, ``fsync``s it, then
  ``os.replace``s it over the target. ``os.replace`` is atomic on POSIX and
  Windows, so a reader ever sees either the old file or the new one — never a
  half-written one.
- ``locked`` takes an exclusive advisory lock (``fcntl.flock``) on a sidecar
  ``.lock`` file so a read-modify-write across processes can't interleave. On
  platforms without ``fcntl`` (non-POSIX) it degrades to a no-op — atomic
  ``os.replace`` still prevents torn files; only the cross-process critical
  section is unavailable, which the deployment target (Linux) always has.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

try:  # POSIX only — absent on stock Windows Python
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# json.dump kwargs used everywhere so on-disk format stays identical to before.
_DUMP_KW: dict[str, Any] = {"indent": 2}

StrPath = "str | os.PathLike[str]"


def read_json(path: StrPath, default: Any) -> Any:
    """Load JSON from ``path``, returning ``default`` if it is missing or corrupt.

    ``default`` is returned (not mutated) so callers can pass a fresh literal.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        logger.warning("read_json: %s is unreadable/corrupt; using default", path)
        return default


def write_json(path: StrPath, obj: Any, *, default: Callable[[Any], Any] | None = None) -> None:
    """Atomically write ``obj`` as JSON to ``path`` (temp file + fsync + replace).

    ``default`` maps to ``json.dump(default=...)`` for non-JSON types (e.g. the
    ``datetime`` values in strategy_state.json).
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, default=default, **_DUMP_KW)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except OSError:
        logger.exception("write_json failed for %s", path)
        _quiet_remove(tmp)
        raise


@contextmanager
def locked(path: StrPath) -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of the block.

    The lock is keyed on ``<path>.lock`` so concurrent read-modify-write cycles
    on the same state file serialise. No-op where ``fcntl`` is unavailable.
    """
    if fcntl is None:  # pragma: no cover - platform-dependent
        yield
        return
    lock_path = f"{os.fspath(path)}.lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of the directory so the rename itself is durable."""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # directories can't be fsync'd on some filesystems
        pass


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
