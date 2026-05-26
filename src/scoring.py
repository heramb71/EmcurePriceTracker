from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from src.types import ScoreBreakdown, ScoreResult

logger = logging.getLogger(__name__)

# Time horizon for each target — how many trading days to allow
_HORIZON: dict[float, int] = {2.0: 3, 5.0: 5, 7.0: 10, 10.0: 15}


def _build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-bar rolling features from daily OHLCV for ML training."""
    close = df["close"]
    volume = df["volume"]

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_hist_col = macd_line - macd_line.ewm(span=9, adjust=False).mean()

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_range = (2 * std20 * 2).replace(0, np.nan)  # upper - lower = 4*std
    bb_pct = ((close - (sma20 - 2 * std20)) / (4 * std20.replace(0, np.nan))).clip(0.0, 1.0)

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    avg_vol = volume.rolling(20).mean().replace(0, np.nan)

    return pd.DataFrame(
        {
            "rsi": rsi,
            "macd_hist": macd_hist_col,
            "bb_pct": bb_pct,
            "above_ema20": (close > ema20).astype(float),
            "ema_cross": (ema20 > ema50).astype(float),
            "vol_ratio": (volume / avg_vol).clip(upper=3.0),
            "mom_5d": close.pct_change(5) * 100,
        },
        index=df.index,
    )


def _build_labels(
    df: pd.DataFrame, target_pct: float, stop_pct: float, horizon: int
) -> np.ndarray:
    """Binary win/loss label for each bar: 1 if target hit before stop within horizon."""
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    labels = np.full(n, np.nan)

    for i in range(n - horizon):
        entry = close[i]
        if entry <= 0:
            continue
        target_price = entry * (1 + target_pct / 100)
        stop_price = entry * (1 - stop_pct / 100)
        hit = False
        for j in range(i + 1, min(i + horizon + 1, n)):
            if low[j] <= stop_price:
                break
            if high[j] >= target_price:
                hit = True
                break
        labels[i] = 1.0 if hit else 0.0

    return labels


def compute_ml_target_probabilities(
    df: pd.DataFrame,
    today_features: dict,
    targets_pct: list[float],
    stop_pct: float = 2.0,
) -> dict[float, int]:
    """
    Train a logistic regression per target on the last 250 bars, then predict
    the probability of hitting each target conditioned on today's signal state.

    Falls back to the historical hit-rate when sklearn is unavailable or there
    are fewer than 20 labelled samples / only one class in the training window.
    """
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return compute_target_probabilities(df, targets_pct, stop_pct)

    window = df.tail(250).reset_index(drop=True)
    feature_df = _build_feature_matrix(window)
    feature_cols = ["rsi", "macd_hist", "bb_pct", "above_ema20", "ema_cross", "vol_ratio", "mom_5d"]

    today_vec = np.array(
        [[
            today_features.get("rsi", 50.0),
            today_features.get("macd_hist", 0.0),
            today_features.get("bb_pct", 0.5),
            float(today_features.get("above_ema20", 0)),
            float(today_features.get("ema_cross", 0)),
            today_features.get("vol_ratio", 1.0),
            today_features.get("mom_5d", 0.0),
        ]]
    )

    results: dict[float, int] = {}

    for target in targets_pct:
        horizon = _HORIZON.get(float(target), 5)
        labels = _build_labels(window, float(target), stop_pct, horizon)

        valid = ~np.isnan(labels)
        X = feature_df[feature_cols][valid]
        y = labels[valid]

        feat_valid = X.notna().all(axis=1)
        X = X[feat_valid]
        y = y[feat_valid.values]

        if len(X) < 20 or len(np.unique(y)) < 2:
            results[float(target)] = round(float(np.mean(y)) * 100) if len(y) > 0 else 0
            continue

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        today_df = pd.DataFrame(today_vec, columns=feature_cols)
        today_scaled = scaler.transform(today_df)

        model = LogisticRegression(C=1.0, max_iter=500, random_state=42)
        model.fit(X_scaled, y)

        prob = model.predict_proba(today_scaled)[0][1]
        results[float(target)] = round(prob * 100)

    return results


def compute_target_probabilities(
    df: pd.DataFrame,
    targets_pct: list[float],
    stop_pct: float = 2.0,
) -> dict[float, int]:
    """
    For each target return the historical hit-rate (0–100) using the last 250 bars.

    A trade "wins" if price touches +target% HIGH before -stop_pct% LOW within the
    mapped horizon.  A trade "loses" if the stop is hit first or the horizon expires
    without touching the target.  The ratio wins/total is the reported probability.

    Horizons: +2% → 3 days, +5% → 5 days, +7% → 10 days, +10% → 15 days.
    """
    window = df.tail(250).reset_index(drop=True)
    n = len(window)
    results: dict[float, int] = {}

    for target in targets_pct:
        horizon = _HORIZON.get(float(target), 5)
        hits = 0
        total = 0

        for i in range(n - horizon):
            entry = float(window["close"].iloc[i])
            target_price = entry * (1 + target / 100)
            stop_price = entry * (1 - stop_pct / 100)
            hit = False

            for j in range(i + 1, min(i + horizon + 1, n)):
                high = float(window["high"].iloc[j])
                low = float(window["low"].iloc[j])
                if low <= stop_price:  # stopped out first → miss
                    break
                if high >= target_price:
                    hit = True
                    break

            if hit:
                hits += 1
            total += 1

        results[float(target)] = round(hits / total * 100) if total > 0 else 0

    return results


def compute_intraday_probabilities(
    df_intraday: Optional[pd.DataFrame],
    targets_pct: list[float],
    stop_pct: float = 0.5,
) -> dict[str | float, int]:
    """
    Single-pass simulation over intraday sessions.

    Returns target hit-rates keyed by float (e.g. {0.5: 72, 1.0: 48, 1.5: 31})
    plus the stop-fire rate keyed by the string "stop_hit" (e.g. {"stop_hit": 18}).

    For each bar in the first 70% of each session, classifies the trade outcome:
      win  — HIGH reaches +target% before LOW reaches -stop_pct%
      loss — LOW reaches -stop_pct% before any target is hit
      timeout — neither hit before session end
    """
    empty: dict[str | float, int] = {float(t): 0 for t in targets_pct}
    empty["stop_hit"] = 0
    if df_intraday is None or df_intraday.empty:
        return empty

    df = df_intraday.copy()
    df["session"] = pd.to_datetime(df["date"]).dt.date

    # Accumulators: one per target + one for stop-hit
    target_hits = {float(t): 0 for t in targets_pct}
    stop_hits = 0
    total = 0

    for _, session_df in df.groupby("session"):
        bars = session_df.reset_index(drop=True)
        n = len(bars)
        cutoff = max(1, int(n * 0.70))

        for i in range(cutoff):
            entry = float(bars["close"].iloc[i])
            if entry <= 0:
                continue

            stop_price = entry * (1 - stop_pct / 100)
            target_prices = {t: entry * (1 + t / 100) for t in targets_pct}

            # Track which targets are still alive for this entry
            alive = set(target_prices.keys())
            stopped = False

            for j in range(i + 1, n):
                high = float(bars["high"].iloc[j])
                low = float(bars["low"].iloc[j])

                if low <= stop_price:
                    stopped = True
                    break

                for t in list(alive):
                    if high >= target_prices[t]:
                        target_hits[t] += 1
                        alive.discard(t)

                if not alive:
                    break

            if stopped:
                stop_hits += 1
            total += 1

    results: dict[str | float, int] = {
        float(t): round(target_hits[t] / total * 100) if total > 0 else 0
        for t in targets_pct
    }
    results["stop_hit"] = round(stop_hits / total * 100) if total > 0 else 0
    return results


def detect_regime(df: pd.DataFrame) -> str:
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError:
        return "Unknown"

    try:
        window = df.tail(60).copy()
        if len(window) < 10:
            return "Unknown"

        returns = window["close"].pct_change().dropna()
        volatility = returns.rolling(5).std().dropna()

        n = min(len(returns), len(volatility))
        ret_vals = returns.iloc[-n:].values
        vol_vals = volatility.iloc[-n:].values

        X = np.column_stack([ret_vals, vol_vals])

        model = GaussianHMM(
            n_components=3, covariance_type="diag", n_iter=100, random_state=42
        )
        model.fit(X)

        states = model.predict(X)
        means = model.means_[:, 0]  # mean return per state

        sorted_states = np.argsort(means)
        state_labels = {
            int(sorted_states[0]): "Trending Down",
            int(sorted_states[1]): "Ranging",
            int(sorted_states[2]): "Trending Up",
        }

        return state_labels.get(int(states[-1]), "Unknown")
    except Exception:
        logger.exception("detect_regime failed")
        return "Unknown"


def compute_score(
    quote: dict,
    pivots: dict,
    cam: dict,
    atr_lvls: dict,
    rsi: float,
    macd_hist: float,
    vwap: float,
    ema20: float,
    ema50: float,
    sentiment: dict,
    avg_volume: int,
    regime: str,
) -> dict:
    """
    Compute trading score and signal.

    Returns a dict for backward compatibility with main.py.
    Use ScoreResult.from_dict() to get a type-safe version.
    """
    weights = {
        "pivot": 0.25,
        "rsi": 0.20,
        "macd": 0.15,
        "vwap": 0.20,
        "sentiment": 0.10,
        "volume": 0.10,
    }

    if regime == "Trending Up":
        weights["macd"] += 0.05
        weights["pivot"] -= 0.05
    elif regime == "Trending Down":
        weights["rsi"] += 0.05
        weights["vwap"] -= 0.05
    elif regime == "Ranging":
        weights["pivot"] += 0.10
        weights["macd"] -= 0.10

    price = quote.get("price", 0.0)

    # Pivot sub-score: near S1/S2 = strong buy zone; near/above R1/R2 = resistance
    pivot_score = _pivot_sub_score(price, pivots)

    # RSI sub-score: oversold = 1.0, overbought = 0.0, linear in between
    if rsi <= 30:
        rsi_score = 1.0
    elif rsi >= 70:
        rsi_score = 0.0
    elif rsi <= 50:
        rsi_score = 0.5 + (50 - rsi) / 40
    else:
        rsi_score = 0.5 - (rsi - 50) / 40
    rsi_score = round(max(0.0, min(1.0, rsi_score)), 3)

    # MACD histogram: positive = bullish momentum
    macd_score = 1.0 if macd_hist > 0 else 0.0

    # VWAP sub-score: price above VWAP = bullish intraday
    vwap_score = 1.0 if (vwap > 0 and price > vwap) else (0.5 if vwap == 0 else 0.0)

    # Sentiment sub-score: map [-1, +1] score to [0, 1]
    raw_sent = sentiment.get("score", 0.0)
    sentiment_score = round(max(0.0, min(1.0, (raw_sent + 1.0) / 2)), 3)

    # Volume sub-score: higher vs average = stronger conviction
    volume = quote.get("volume", 0)
    vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0
    volume_score = round(min(1.0, vol_ratio / 2), 3)

    # Create typed breakdown
    breakdown = ScoreBreakdown(
        pivot=pivot_score,
        rsi=rsi_score,
        macd=macd_score,
        vwap=vwap_score,
        sentiment=sentiment_score,
        volume=volume_score,
    )

    # Compute final score
    score = round(
        sum(breakdown.to_dict()[k] * weights[k] for k in breakdown.to_dict()), 4
    )

    if score >= 0.70:
        signal, signal_color = "Strong Buy", "bold green"
    elif score >= 0.55:
        signal, signal_color = "Buy", "green"
    elif score <= 0.30:
        signal, signal_color = "Strong Sell", "bold red"
    elif score <= 0.45:
        signal, signal_color = "Sell", "red"
    else:
        signal, signal_color = "Hold", "yellow"

    # Create typed result and convert back to dict for backward compatibility
    result = ScoreResult(
        score=score,
        signal=signal,
        signal_color=signal_color,
        entry=atr_lvls.get("entry", 0.0),
        sl=atr_lvls.get("sl", 0.0),
        t1=atr_lvls.get("t1", 0.0),
        t2=atr_lvls.get("t2", 0.0),
        regime=regime,
        breakdown=breakdown,
    )
    return result.to_dict()


def _pivot_sub_score(price: float, pivots: dict) -> float:
    s2, s1 = pivots["S2"], pivots["S1"]
    pp = pivots["PP"]
    r1, r2 = pivots["R1"], pivots["R2"]

    if price < s2:
        return 0.9  # Deep support — strong mean-reversion buy
    elif price <= s1:
        return 1.0  # Classic S1–S2 entry zone
    elif price < pp:
        return 0.65  # Between S1 and PP — mildly bullish
    elif price <= r1:
        return 0.5  # PP–R1 — neutral / wait
    elif price <= r2:
        return 0.3  # R1–R2 — approaching resistance
    return 0.1  # Above R2 — extended, caution
