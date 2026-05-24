from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from emcure_tracker import config
from emcure_tracker.indicators import IndicatorResult

logger = logging.getLogger(__name__)

# ── Module-level model state (populated by background thread) ──────────────
_regime_model = None       # fitted HMM or KMeans
_ensemble_model = None     # fitted XGBoost regressor
_models_ready = False


@dataclass(frozen=True)
class ForecastResult:
    mid: float
    low: float
    high: float
    bias_pct: float
    signal: str
    sig_color: str
    conviction: float
    avg_range: float
    regime: str              # 'trending' | 'ranging' | 'reverting' | 'unknown'


# ── Background init (called once at startup) ───────────────────────────────

def init_models_background() -> None:
    global _regime_model, _ensemble_model, _models_ready
    try:
        from emcure_tracker.data.market import fetch_ohlcv
        ohlcv = fetch_ohlcv()
        if ohlcv is None or len(ohlcv.df) < 60:
            logger.warning("Not enough data to train forecast models")
            return

        df = ohlcv.df.copy()
        _regime_model = _fit_regime(df)
        _ensemble_model = _fit_ensemble(df)
        _models_ready = True
        logger.warning("Forecast models ready.")
    except Exception:
        logger.exception("init_models_background failed")


# ── Regime detection ───────────────────────────────────────────────────────

def _fit_regime(df: pd.DataFrame):
    try:
        from sklearn.cluster import KMeans  # type: ignore
        from joblib import parallel_backend  # type: ignore
        features = _regime_features(df)
        if features is None:
            return None
        km = KMeans(n_clusters=3, random_state=42, n_init=10)
        # threading backend avoids loky multiprocessing crash on macOS ARM + Python 3.13
        with parallel_backend("threading", n_jobs=1):
            km.fit(features)
        return km
    except Exception:
        logger.exception("_fit_regime failed")
        return None


def _regime_features(df: pd.DataFrame) -> Optional[np.ndarray]:
    close = df["close"]
    if len(close) < 30:
        return None
    returns = close.pct_change().dropna()
    volatility = returns.rolling(10).std().dropna()
    trend = close.rolling(10).mean().pct_change().dropna()
    min_len = min(len(volatility), len(trend))
    return np.column_stack([
        volatility.iloc[-min_len:].values,
        trend.iloc[-min_len:].values,
    ])


def _detect_regime(df: pd.DataFrame) -> str:
    if _regime_model is None:
        return "unknown"
    try:
        features = _regime_features(df)
        if features is None or len(features) == 0:
            return "unknown"
        last = features[-1].reshape(1, -1)
        cluster = int(_regime_model.predict(last)[0])
        centers = _regime_model.cluster_centers_
        # Interpret clusters by volatility level
        vol_order = np.argsort(centers[:, 0])
        if cluster == vol_order[0]:
            return "ranging"
        elif cluster == vol_order[2]:
            return "reverting"
        return "trending"
    except Exception:
        return "unknown"


# ── XGBoost ensemble ───────────────────────────────────────────────────────

def _fit_ensemble(df: pd.DataFrame):
    try:
        from xgboost import XGBRegressor  # type: ignore
        X, y = _build_features(df)
        if X is None or len(X) < 30:
            return None
        model = XGBRegressor(n_estimators=100, max_depth=4, learning_rate=0.05,
                             random_state=42, verbosity=0)
        model.fit(X[:-1], y[:-1])
        return model
    except Exception:
        logger.exception("_fit_ensemble failed")
        return None


def _build_features(df: pd.DataFrame):
    close = df["close"]
    if len(close) < 40:
        return None, None
    rsi = _rolling_rsi(close)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    returns = close.pct_change()
    vol = returns.rolling(10).std()
    X = pd.DataFrame({
        "rsi": rsi,
        "ema_ratio": ema20 / ema50,
        "vol": vol,
        "ret_1": returns,
        "ret_3": close.pct_change(3),
    }).dropna()
    y = close.pct_change().shift(-1).loc[X.index].dropna()
    X = X.loc[y.index]
    return X.values, y.values


def _rolling_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ensemble_bias(df: pd.DataFrame) -> float:
    if _ensemble_model is None:
        return 0.0
    try:
        X, _ = _build_features(df)
        if X is None or len(X) == 0:
            return 0.0
        pred = float(_ensemble_model.predict(X[-1].reshape(1, -1))[0])
        return round(pred * 100, 3)   # convert fraction → %
    except Exception:
        return 0.0


# ── Hand-tuned bias (fallback when models not ready) ──────────────────────

def _manual_bias(indicators: IndicatorResult, sentiment_score: float) -> float:
    rsi = indicators.rsi
    hist = indicators.macd_hist
    ema_s = indicators.ema_short
    ema_l = indicators.ema_long
    price = indicators.bb_mid   # approximate current price

    rsi_bias = 0.0
    if rsi < 30:
        rsi_bias = +0.5
    elif rsi < 40:
        rsi_bias = +0.25
    elif rsi > 70:
        rsi_bias = -0.5
    elif rsi > 60:
        rsi_bias = -0.25

    macd_bias = +0.3 if hist > 0 else -0.3
    trend_bias = +0.2 if ema_s > ema_l else -0.2

    bb_bias = 0.0
    if price < indicators.bb_lower:
        bb_bias = +0.4
    elif price > indicators.bb_upper:
        bb_bias = -0.4

    sent_bias = sentiment_score * 0.8
    return rsi_bias + macd_bias + trend_bias + bb_bias + sent_bias


# ── Public entry point ─────────────────────────────────────────────────────

def compute_forecast(
    df: pd.DataFrame,
    indicators: IndicatorResult,
    sentiment_score: float,
    vol_ratio: float,
) -> Optional[ForecastResult]:
    try:
        regime = _detect_regime(df)

        if _models_ready:
            bias_pct = _ensemble_bias(df)
        else:
            bias_pct = _manual_bias(indicators, sentiment_score)

        conviction = min(1.5, 1.0 + (vol_ratio - 1.0) * 0.3)
        total_bias = bias_pct * conviction
        current = indicators.bb_mid
        half_range = indicators.avg_range * 0.6
        forecast_mid = current * (1 + total_bias / 100)

        if total_bias >= 1.0:
            signal, sig_color = "Strong Buy", "bold green"
        elif total_bias >= 0.3:
            signal, sig_color = "Buy", "green"
        elif total_bias <= -1.0:
            signal, sig_color = "Strong Sell", "bold red"
        elif total_bias <= -0.3:
            signal, sig_color = "Sell", "red"
        else:
            signal, sig_color = "Hold / Wait", "yellow"

        return ForecastResult(
            mid=round(forecast_mid, 2),
            low=round(forecast_mid - half_range, 2),
            high=round(forecast_mid + half_range, 2),
            bias_pct=round(total_bias, 3),
            signal=signal,
            sig_color=sig_color,
            conviction=round(conviction, 2),
            avg_range=indicators.avg_range,
            regime=regime,
        )
    except Exception:
        logger.exception("compute_forecast failed")
        return None
