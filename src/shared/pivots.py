from __future__ import annotations


def classic_pivots(high: float, low: float, close: float) -> dict:
    pp = (high + low + close) / 3
    return {
        "PP": round(pp, 2),
        "R1": round(2 * pp - low, 2),
        "R2": round(pp + (high - low), 2),
        "R3": round(high + 2 * (pp - low), 2),
        "S1": round(2 * pp - high, 2),
        "S2": round(pp - (high - low), 2),
        "S3": round(low - 2 * (high - pp), 2),
    }


def camarilla_pivots(high: float, low: float, close: float) -> dict:
    rng = high - low
    return {
        "H4": round(close + 1.1 * rng / 2, 2),
        "H3": round(close + 1.1 * rng / 4, 2),
        "L3": round(close - 1.1 * rng / 4, 2),
        "L4": round(close - 1.1 * rng / 2, 2),
    }


def pivot_signal(price: float, pivots: dict) -> str:
    s3, s2, s1 = pivots["S3"], pivots["S2"], pivots["S1"]
    pp = pivots["PP"]
    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]

    if price < s3:
        return "Below S3"
    elif price < s2:
        return "S3–S2"
    elif price < s1:
        return "S2–S1"
    elif price < pp:
        return "S1–PP"
    elif price < r1:
        return "PP–R1"
    elif price < r2:
        return "R1–R2"
    elif price < r3:
        return "R2–R3"
    return "Above R3"


def atr_levels(price: float, atr: float) -> dict:
    return {
        "entry": price,
        "sl": round(price - 1.5 * atr, 2),
        "t1": round(price + 1.5 * atr, 2),
        "t2": round(price + 3.0 * atr, 2),
        "atr": round(atr, 2),
    }
