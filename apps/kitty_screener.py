"""KittyBot pre-market screener — ranks the kitty universe into daily_picks.json.

Runs once pre-market (08:45 IST via emcure-kitty-screener.timer, or by hand):
fetches daily bars for every symbol in the configured kitty universe, computes
liquidity/volatility/2%-reachability stats, ranks the top-N, and atomically writes
``daily_picks.json`` for the trading bot (``apps.kittybot_headless``) to consume.

This is the "separate screener" the bot expects; the bot degrades to its fallback
universe if this file is missing or stale, so a screener failure never blocks the
day — it just costs the ranked edge.

Run:  python -m apps.kitty_screener            # write daily_picks.json
      python -m apps.kitty_screener --dry-run  # print the ranking, write nothing
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from src.kittybot import screener
from src.kittybot.config import load_config
from src.shared.atomic_json import write_json
from src.shared.data import fetch_daily

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("kitty_screener")

_IST = timezone(timedelta(hours=5, minutes=30))


def run(dry_run: bool = False) -> dict:
    """Screen the universe and (unless dry-run) write daily_picks.json. Returns
    the payload so callers/tests can inspect it."""
    cfg = load_config()
    universe = cfg.fallback_universe
    logger.info("Screening %d symbols (ADTV ≥ ₹%.0f Cr, lookback %dd) ...",
                len(universe), cfg.screen_min_adtv_cr, cfg.screen_lookback_days)

    metrics: list[screener.ScreenMetrics] = []
    for symbol in universe:
        df = fetch_daily(symbol, days=max(cfg.screen_lookback_days + 20, 100))
        m = screener.screen_symbol(symbol, df, cfg)
        if m is None:
            logger.info("  %-12s skipped (thin data or below liquidity floor)", symbol)
            continue
        metrics.append(m)
        logger.info("  %-12s score=%5.1f hit2%%=%4.1f range=%4.2f%% adtv=₹%.0fCr",
                    symbol, m.score, m.hit_rate_2pct, m.avg_range_60d_pct, m.adtv_cr)

    ranked = screener.rank(metrics, cfg.max_picks)
    payload = screener.build_payload(ranked, len(universe), datetime.now(_IST))

    logger.info("Top %d: %s", len(ranked),
                ", ".join(f"{m.symbol}({m.score:.0f})" for m in ranked) or "—")
    if dry_run:
        logger.info("Dry run — %s not written.", cfg.picks_path)
        return payload
    write_json(cfg.picks_path, payload)
    logger.info("Wrote %d picks → %s", len(ranked), cfg.picks_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="KittyBot pre-market screener")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ranking without writing daily_picks.json")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
