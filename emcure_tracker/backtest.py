from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from emcure_tracker import config

logger = logging.getLogger(__name__)

# ── Shared state written by background thread, read by display ─────────────
_result: Optional[BacktestResult] = None
_ready = threading.Event()


@dataclass(frozen=True)
class BacktestResult:
    win_rate_pct: float
    avg_return_pct: float
    max_drawdown_pct: float
    total_trades: int
    sharpe: float
    period_days: int
    available: bool


# ── VectorBT backtest ──────────────────────────────────────────────────────

def run_backtest_background() -> None:
    global _result
    try:
        from emcure_tracker.data.market import fetch_ohlcv
        ohlcv = fetch_ohlcv()
        if ohlcv is None or len(ohlcv.df) < 60:
            logger.warning("Insufficient data for backtest")
            _result = _unavailable()
            _ready.set()
            return

        df = ohlcv.df.copy()
        _result = _run_vbt(df)
    except Exception:
        logger.exception("run_backtest_background failed")
        _result = _unavailable()
    finally:
        _ready.set()


def _run_vbt(df: pd.DataFrame) -> BacktestResult:
    try:
        import vectorbt as vbt  # type: ignore

        close = df.set_index("date")["close"]

        # Simple RSI crossover strategy: buy on oversold (RSI < 30), sell on overbought (RSI > 70)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(config.RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(config.RSI_PERIOD).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        entries = rsi < 30
        exits = rsi > 70

        pf = vbt.Portfolio.from_signals(
            close,
            entries,
            exits,
            init_cash=100_000,
            freq="D",
        )

        stats = pf.stats()
        total_return = float(stats.get("Total Return [%]", 0))
        max_dd = float(stats.get("Max Drawdown [%]", 0))
        total_trades = int(stats.get("Total Trades", 0))
        win_rate = float(stats.get("Win Rate [%]", 0))
        sharpe = float(stats.get("Sharpe Ratio", 0))
        avg_ret = total_return / total_trades if total_trades > 0 else 0.0

        return BacktestResult(
            win_rate_pct=round(win_rate, 1),
            avg_return_pct=round(avg_ret, 2),
            max_drawdown_pct=round(abs(max_dd), 1),
            total_trades=total_trades,
            sharpe=round(sharpe, 2),
            period_days=len(df),
            available=True,
        )

    except ImportError:
        logger.warning("vectorbt not installed — backtest unavailable")
        return _unavailable()
    except Exception:
        logger.exception("_run_vbt failed")
        return _unavailable()


def _unavailable() -> BacktestResult:
    return BacktestResult(
        win_rate_pct=0.0,
        avg_return_pct=0.0,
        max_drawdown_pct=0.0,
        total_trades=0,
        sharpe=0.0,
        period_days=0,
        available=False,
    )


def get_result() -> Optional[BacktestResult]:
    return _result


def is_ready() -> bool:
    return _ready.is_set()
