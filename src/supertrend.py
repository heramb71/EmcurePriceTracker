from __future__ import annotations

import pandas as pd


def compute_supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """
    Supertrend indicator.

    Returns a DataFrame aligned to `df` with columns:
      - supertrend: the trailing line value
      - direction:  +1 when price is in an uptrend (line below price)
                    -1 when price is in a downtrend (line above price)
      - atr:        the ATR series used in the calculation
    """
    if len(df) < period + 1:
        return pd.DataFrame(
            {
                "supertrend": pd.Series(dtype=float),
                "direction": pd.Series(dtype=int),
                "atr": pd.Series(dtype=float),
            }
        )

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()

    hl2 = (high + low) / 2
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper = upper_basic.copy()
    lower = lower_basic.copy()

    for i in range(1, len(df)):
        if pd.notna(upper.iloc[i]) and pd.notna(upper.iloc[i - 1]):
            if (
                upper_basic.iloc[i] < upper.iloc[i - 1]
                or close.iloc[i - 1] > upper.iloc[i - 1]
            ):
                upper.iloc[i] = upper_basic.iloc[i]
            else:
                upper.iloc[i] = upper.iloc[i - 1]

        if pd.notna(lower.iloc[i]) and pd.notna(lower.iloc[i - 1]):
            if (
                lower_basic.iloc[i] > lower.iloc[i - 1]
                or close.iloc[i - 1] < lower.iloc[i - 1]
            ):
                lower.iloc[i] = lower_basic.iloc[i]
            else:
                lower.iloc[i] = lower.iloc[i - 1]

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    first_valid = atr.first_valid_index()
    if first_valid is None:
        return pd.DataFrame(
            {"supertrend": supertrend, "direction": direction, "atr": atr}
        )

    start = df.index.get_loc(first_valid)
    supertrend.iloc[start] = upper.iloc[start]
    direction.iloc[start] = -1

    for i in range(start + 1, len(df)):
        prev_st = supertrend.iloc[i - 1]
        # If previous line was the upper band, we were in a downtrend
        if prev_st == upper.iloc[i - 1]:
            if close.iloc[i] > upper.iloc[i]:
                supertrend.iloc[i] = lower.iloc[i]
                direction.iloc[i] = 1
            else:
                supertrend.iloc[i] = upper.iloc[i]
                direction.iloc[i] = -1
        else:
            if close.iloc[i] < lower.iloc[i]:
                supertrend.iloc[i] = upper.iloc[i]
                direction.iloc[i] = -1
            else:
                supertrend.iloc[i] = lower.iloc[i]
                direction.iloc[i] = 1

    return pd.DataFrame(
        {"supertrend": supertrend, "direction": direction, "atr": atr}, index=df.index
    )
