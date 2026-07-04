"""KittyBot configuration — every threshold in one typed, frozen place.

Values load from ``config/kittybot.toml`` (a flat set of ``[section]`` tables that
are merged into one dict), fall back to the dataclass defaults when the file or a
key is absent, and are then overridden by a small whitelist of environment
variables for the deployment- and safety-critical knobs (``KITTYBOT_LIVE``,
``KITTYBOT_BROKER``, ``KITTYBOT_CAPITAL`` …). This keeps thresholds in a checked-in
config file while letting the server flip paper→live without editing the repo.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import time as dtime
from pathlib import Path

logger = logging.getLogger(__name__)

# Fallback kitty universe, used only when daily_picks.json is missing/unreadable.
# High-beta PSU / metals / thematic names that routinely move 2–5% intraday.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "ADANIENT", "ADANIGREEN", "ADANIPOWER", "ADANIENSOL", "TATAMOTORS", "VEDL",
    "JINDALSTEL", "HINDALCO", "TATASTEEL", "BANKBARODA", "CANBK", "PNB",
    "INDUSINDBK", "BEL", "HAL", "CGPOWER", "TATAPOWER", "JSWENERGY", "TRENT",
    "IRFC", "PFC", "RECLTD", "ETERNAL", "SWIGGY", "LODHA", "DLF", "DIXON",
    "MOTHERSON", "SHRIRAMFIN", "JIOFIN", "NAUKRI", "VBL",
)

# Default config file location (repo-root/config/kittybot.toml).
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "kittybot.toml"


@dataclass(frozen=True)
class KittyBotConfig:
    # ── capital & risk ────────────────────────────────────────────────────────
    capital: float = 100_000.0        # trading capital in ₹
    risk_per_trade_pct: float = 1.0   # max % of capital risked per trade
    reward_risk_ratio: float = 2.0    # target : stop (2:1 → stop = half the target)

    # ── the daily kitty ───────────────────────────────────────────────────────
    picks_path: str = "daily_picks.json"
    picks_max_age_hours: float = 24.0  # skip the day if the file is older than this
    max_picks: int = 5                 # top-N candidates to consider
    fallback_universe: tuple[str, ...] = DEFAULT_UNIVERSE

    # ── entry filters ─────────────────────────────────────────────────────────
    gap_max_pct: float = 1.5           # discard picks gapping > this vs prev close

    # ── screener (kitty_screener.py → daily_picks.json) ───────────────────────
    screen_min_adtv_cr: float = 100.0  # liquidity floor (₹ crore ADTV) to qualify
    screen_lookback_days: int = 60     # window for range / hit-rate / room stats

    # ── opening range / breakout ──────────────────────────────────────────────
    opening_range_minutes: int = 15
    breakout_volume_multiple: float = 1.0  # breakout bar volume ≥ this × average

    # ── timing (IST wall-clock, "HH:MM") ──────────────────────────────────────
    observe_start: str = "09:15"       # opening range starts (no trading inside it)
    select_time: str = "09:30"         # earliest a trigger may be selected
    no_trade_after: str = "10:30"      # no new entries after this
    hard_exit: str = "15:10"           # force-close any open position at/after this
    breakeven_trigger_pct: float = 1.0  # move stop to breakeven at + this %

    # ── safety rails ──────────────────────────────────────────────────────────
    vix_spike_pct: float = 15.0        # skip day if India VIX up > this % at select
    max_consecutive_losing_days: int = 3  # then halt, resume next week

    # ── execution ─────────────────────────────────────────────────────────────
    broker: str = "paper"              # paper | kite | upstox
    live: bool = False                 # real orders ONLY when live AND broker != paper
    product: str = "MIS"               # intraday product

    # ── plumbing ──────────────────────────────────────────────────────────────
    telegram_service: str = "radar"    # reuse the radar Telegram feed (scanner retired)
    journal_dir: str = "kittybot_journal"
    state_path: str = "kittybot_state.json"

    # ── derived helpers (pure) ────────────────────────────────────────────────
    @property
    def observe_start_t(self) -> dtime:
        return parse_hhmm(self.observe_start)

    @property
    def select_time_t(self) -> dtime:
        return parse_hhmm(self.select_time)

    @property
    def no_trade_after_t(self) -> dtime:
        return parse_hhmm(self.no_trade_after)

    @property
    def hard_exit_t(self) -> dtime:
        return parse_hhmm(self.hard_exit)

    @property
    def sends_real_orders(self) -> bool:
        """True only when a live broker will actually place money orders."""
        return self.live and self.broker != "paper"


def parse_hhmm(value: str) -> dtime:
    """Parse an ``"HH:MM"`` wall-clock string into a ``datetime.time``."""
    hh, mm = value.strip().split(":")
    return dtime(int(hh), int(mm))


# ── loading ───────────────────────────────────────────────────────────────────

# env var → (field name, caster). Only safety/deploy-critical knobs are exposed.
_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "KITTYBOT_CAPITAL": ("capital", float),
    "KITTYBOT_BROKER": ("broker", str),
    "KITTYBOT_LIVE": ("live", bool),
    "KITTYBOT_PICKS_PATH": ("picks_path", str),
    "KITTYBOT_RISK_PCT": ("risk_per_trade_pct", float),
}

_TRUE = {"1", "true", "yes", "on"}


def _cast(raw: str, typ: type):
    if typ is bool:
        return raw.strip().lower() in _TRUE
    return typ(raw)


def _flatten_toml(data: dict) -> dict:
    """Merge ``[section]`` tables into one flat dict of known field values."""
    valid = {f.name for f in fields(KittyBotConfig)}
    flat: dict = {}
    for key, val in data.items():
        if isinstance(val, dict):
            for k, v in val.items():
                if k in valid:
                    flat[k] = v
        elif key in valid:  # top-level key (no section)
            flat[key] = val
    # TOML arrays → tuples for frozen/hashable fields.
    if "fallback_universe" in flat:
        flat["fallback_universe"] = tuple(str(s).upper() for s in flat["fallback_universe"])
    return flat


def load_config(path: str | os.PathLike[str] | None = None) -> KittyBotConfig:
    """Build a :class:`KittyBotConfig` from the TOML file, then apply env overrides.

    A missing file is not an error — the dataclass defaults are a complete, safe
    (paper-trading) configuration on their own.
    """
    cfg_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    values: dict = {}
    try:
        with open(cfg_path, "rb") as fh:
            values = _flatten_toml(tomllib.load(fh))
    except FileNotFoundError:
        logger.info("kittybot config %s not found — using defaults", cfg_path)
    except (tomllib.TOMLDecodeError, OSError):
        logger.exception("kittybot config %s unreadable — using defaults", cfg_path)

    cfg = replace(KittyBotConfig(), **values) if values else KittyBotConfig()

    overrides: dict = {}
    for env_key, (field_name, typ) in _ENV_OVERRIDES.items():
        raw = os.getenv(env_key)
        if raw is not None and raw != "":
            try:
                overrides[field_name] = _cast(raw, typ)
            except ValueError:
                logger.warning("ignoring bad %s=%r", env_key, raw)
    return replace(cfg, **overrides) if overrides else cfg
