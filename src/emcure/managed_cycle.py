"""
Managed delivery-position auto-cycle for a single symbol (EMCURE).

Replaces the Supertrend auto-trade when MANAGED_CYCLE=true. Each refresh cycle it
makes ONE decision:

  Holding  → SELL the full position at the HIGHEST target reachable today
             (judged by the day's ATR/range), or EXIT at the stop-loss.
  Flat     → RE-ENTER on an SMA7 mean-reversion dip (price a set amount below the
             7-day SMA, trend not down), then rebuild targets from the new entry.

Targets are anchored to the entry (booked) price as fixed rupee deltas
(default +₹15/+20/+30) with a fixed-rupee stop (default −₹100).

SAFETY: two phases.
  Phase 1 (default)  MANAGED_CYCLE_LIVE unset/false → DRY-RUN. Decisions are
                     logged and announced ("WOULD SELL 8 @ ₹1763"), but NO order
                     is placed and the real position is never mutated.
  Phase 2            MANAGED_CYCLE_LIVE=true → places real Kite orders via the
                     same place_order_and_confirm path the Supertrend strategy uses.

State lives in managed_state.json (separate from strategy_state.json so the two
strategies never share a record).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.emcure import ledger
from src.shared.atomic_json import locked, read_json, write_json
from src.shared.costs import round_trip_charges

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "managed_state.json")
_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(_IST)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ManagedConfig:
    enabled: bool                 # MANAGED_CYCLE — disables Supertrend when true
    live: bool                    # MANAGED_CYCLE_LIVE — false = dry-run, no orders
    targets: tuple[float, ...]    # rupee deltas from entry, ascending
    sl_rupees: float              # stop = entry − sl_rupees
    qty: int                      # re-entry position size (shares)
    reentry_gap: float            # re-enter when price <= sma7 − reentry_gap
    reach_min_prob: float         # aim for the highest target with reach-prob ≥ this (%)
    max_daily_loss: float         # block new entries once realized day loss ≥ this (₹)
    reentry_cooldown_min: float   # min minutes between an exit and the next entry
    block_reentry_after_stop: bool  # no re-entry the same day as a stop-out
    reentry_gap_pct: float = 0.0  # >0 → gap trigger = sma7 × pct/100 (overrides the ₹ gap)

    @classmethod
    def from_env(cls) -> "ManagedConfig":
        raw = os.getenv("MANAGED_TARGETS", "15,20,30")
        targets = tuple(sorted(float(x) for x in raw.split(",") if x.strip()))
        sl_rupees = float(os.getenv("MANAGED_SL", "100"))
        qty       = int(os.getenv("MANAGED_QTY", "8"))
        return cls(
            enabled          = os.getenv("MANAGED_CYCLE", "false").lower() == "true",
            live             = os.getenv("MANAGED_CYCLE_LIVE", "false").lower() == "true",
            targets          = targets or (15.0, 20.0, 30.0),
            sl_rupees        = sl_rupees,
            qty              = qty,
            reentry_gap      = float(os.getenv("MANAGED_REENTRY_GAP", "20")),
            reach_min_prob   = float(os.getenv("MANAGED_REACH_MIN_PROB", "50")),
            # Default the daily-loss cap to one full stop (sl × qty): after one
            # stop-out the realized loss hits the cap and re-entries halt for the day.
            max_daily_loss   = float(os.getenv("MANAGED_MAX_DAILY_LOSS", str(sl_rupees * qty))),
            reentry_cooldown_min     = float(os.getenv("MANAGED_REENTRY_COOLDOWN_MIN", "60")),
            block_reentry_after_stop = os.getenv("MANAGED_BLOCK_REENTRY_AFTER_STOP", "true").lower() == "true",
            # A fixed ₹ gap means 1.5% sensitivity at ₹1300 but 1.1% at ₹1800.
            # Opt-in percentage mode keeps the trigger scale-invariant (the
            # radar's validated SMA7 threshold is 1.4% for the same reason).
            reentry_gap_pct          = float(os.getenv("MANAGED_REENTRY_GAP_PCT", "0") or 0),
        )


@dataclass(frozen=True)
class Decision:
    action: str                   # hold | sell | exit_sl | reenter | wait
    reason: str = ""
    price: float = 0.0            # target / stop / entry reference price
    qty: int = 0
    label: str = ""              # chosen target label, e.g. "+₹30"


# ─────────────────────────────────────────────────────────────────────────────
# Pure decision logic (no I/O — fully unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def choose_target(entry: float, probs: dict, cfg: ManagedConfig) -> Optional[dict]:
    """Pick the target to aim for, dynamically, from reach-probabilities.

    Among the configured targets, take the HIGHEST-profit one whose probability of
    being reached (from the current price — see probability.daily_reach_probs)
    clears cfg.reach_min_prob; if none clear it, fall back to the single most
    likely target so the position always has a realistic exit. `probs` is keyed by
    the absolute target price. This replaces the old ATR test, which judged a
    target by the day's *range* and so over-reached for the top target."""
    if not cfg.targets:
        return None
    scored = [(d, round(entry + d, 2)) for d in cfg.targets]
    scored = [(d, price, float(probs.get(price, 0))) for d, price in scored]
    reachable = [c for c in scored if c[2] >= cfg.reach_min_prob]
    d, price, p = (max(reachable, key=lambda c: c[0])    # highest profit among likely
                   if reachable else
                   max(scored, key=lambda c: c[2]))       # else the most likely
    return {"delta": d, "price": price, "label": f"+₹{d:.0f}", "prob": round(p)}


def decide(position: Optional[dict], market: dict, cfg: ManagedConfig) -> Decision:
    """One cycle's decision. position carries entry/qty/sl (None = flat); market
    carries price/day_high/day_low/gap/trend_7d.

    The held-position exit is a mechanical touched-target FLOOR, deliberately NOT
    gated on a moving reach-probability forecast — that forecast-chasing was the
    bug that let a touched +₹20 rung slip while the cycle aimed at +₹30 and gave
    everything back. Probability now only feeds the briefing display
    (choose_target / format_levels_block)."""
    price = float(market.get("price", 0) or 0)
    high  = float(market.get("day_high", 0) or 0)
    low   = float(market.get("day_low", 0) or 0)

    if position:
        entry = float(position["entry"])
        qty   = int(position["qty"])
        sl    = float(position["sl"])

        # 1. Capital protection first — stop hit on the day's low or live price.
        if (low and low <= sl) or (price and price <= sl):
            return Decision("exit_sl", reason=f"Hit the safety exit (₹{sl:,.2f})", price=sl, qty=qty)

        # 2. Touched-target floor. Once the day's high prints a rung it becomes a
        #    locked floor: ride above it toward the next rung, but never give a
        #    touched rung back — sell on a pullback to the floor, and sell outright
        #    once the top rung is reached.
        ladder  = sorted(round(entry + d, 2) for d in cfg.targets)
        top     = ladder[-1]
        touched = [t for t in ladder if (high and high >= t) or price >= t]

        if touched:
            floor  = max(touched)
            fdelta = floor - entry
            if floor >= top:
                if price >= top:             # still at/above top — book it outright
                    return Decision(
                        "sell", price=price, qty=qty, label=f"+₹{fdelta:.0f}",
                        reason=f"Reached the target — ₹{floor:,.2f}",
                    )
                # Top rung was touched intraday (via day_high) but price has since
                # pulled back below it — the 5-min cycle missed the peak. Sell now
                # at current price rather than holding through further downside.
                return Decision(
                    "sell", price=price, qty=qty, label=f"+₹{fdelta:.0f}",
                    reason=(f"Hit ₹{floor:,.2f} then pulled back to "
                            f"₹{price:,.2f} — selling to lock it in"),
                )
            if price <= floor:                       # pulled back to a touched rung
                return Decision(
                    "sell", price=price, qty=qty, label=f"+₹{fdelta:.0f}",
                    reason=f"Slipped back to ₹{floor:,.2f} — booking the ₹{fdelta:.0f} gain",
                )
            nxt    = min(t for t in ladder if t > floor)   # above floor → ride up
            ndelta = nxt - entry
            return Decision(
                "hold", price=nxt, qty=qty, label=f"+₹{ndelta:.0f}",
                reason=f"₹{floor:,.2f} locked in — holding for ₹{nxt:,.2f}",
            )

        # 3. No rung touched yet — hold for the first target.
        first  = ladder[0]
        fdelta = first - entry
        return Decision(
            "hold", price=first, qty=qty, label=f"+₹{fdelta:.0f}",
            reason=f"Holding for the first target — ₹{first:,.2f}",
        )

    # Flat → SMA7 mean-reversion re-entry.
    gap   = float(market.get("gap", 0) or 0)          # price − sma7 (negative = below)
    sma7  = float(market.get("sma7", 0) or 0)
    trend = market.get("trend_7d", "")
    threshold = cfg.reentry_gap
    if cfg.reentry_gap_pct > 0 and sma7 > 0:
        threshold = round(sma7 * cfg.reentry_gap_pct / 100, 2)
    if gap <= -threshold and trend != "Downward":
        return Decision(
            "reenter", reason=f"Price is ₹{abs(gap):.0f} below its recent average — buying the dip",
            price=price, qty=cfg.qty,
        )
    downtrend = " (still falling — waiting)" if trend == "Downward" else ""
    return Decision("wait", reason=f"No buy yet — price is ₹{gap:+.0f} from its recent average{downtrend}")


# ─────────────────────────────────────────────────────────────────────────────
# State I/O (own file — never shares with strategy_state.json)
# ─────────────────────────────────────────────────────────────────────────────

def _load() -> dict:
    return read_json(_STATE_FILE, {})


def _save(state: dict) -> None:
    write_json(_STATE_FILE, state)


def _mutate(update: Callable[[dict], None]) -> dict:
    """Locked read-modify-write of the state file. managed_state.json has two
    writers — the tracker loop and the bot_server command handlers (EXIT/HALT/
    RESUME) — so every mutation holds the advisory lock, exactly like
    trade_state.json. Returns the state as written."""
    with locked(_STATE_FILE):
        state = _load()
        update(state)
        _save(state)
        return state


def _set_once(key: str, sig: str) -> bool:
    """Locked compare-and-set of a dedupe signature. True when ``sig`` is new
    (the caller should fire its one-time event), False when already recorded."""
    with locked(_STATE_FILE):
        state = _load()
        if state.get(key) == sig:
            return False
        state[key] = sig
        _save(state)
        return True


def get_position() -> Optional[dict]:
    return _load().get("position")


def _update_position(**fields) -> None:
    """Merge fields into the stored position (e.g. the resting stop's order id)."""
    def _apply(state: dict) -> None:
        pos = state.get("position")
        if pos:
            pos.update(fields)
    _mutate(_apply)


def set_position(entry: float, qty: int, cfg: ManagedConfig) -> dict:
    pos = {
        "entry":            round(float(entry), 2),
        "qty":              int(qty),
        "sl":               round(float(entry) - cfg.sl_rupees, 2),
        "targets":          list(cfg.targets),
        "opened_at":        datetime.now().isoformat(timespec="seconds"),
        "high_since_entry": round(float(entry), 2),
        "low_since_entry":  round(float(entry), 2),
    }
    def _apply(state: dict) -> None:
        state["position"] = pos
    _mutate(_apply)
    return pos


def clear_position() -> None:
    def _apply(state: dict) -> None:
        state.pop("position", None)
    _mutate(_apply)


# ─────────────────────────────────────────────────────────────────────────────
# Daily risk guards (kill-switch + re-entry cooldown)
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_roll_day(now: datetime) -> None:
    """Reset the day's realized-loss tally and stop-out flag at a date change."""
    today = now.date().isoformat()
    with locked(_STATE_FILE):
        state = _load()
        if state.get("day") != today:
            state["day"] = today
            state["realized_pnl_today"] = 0.0
            state["stopped_out_today"] = False
            _save(state)


def _record_exit(pnl: float, is_stop: bool, now: datetime, *,
                 ticker: str = "", exit_price: float = 0.0, live: bool = False,
                 ) -> tuple[float, float]:
    """Atomically close the position and book the exit into the day's tally so the
    kill-switch and cooldown can see it. Also appends the closed round-trip to the
    durable P&L ledger (best-effort — never blocks the exit).

    ``pnl`` is gross; the round-trip charges (STT/txn/stamp/GST + DP) are
    computed here so the day tally, the kill-switch, and the ledger all run on
    NET money. Returns ``(net_pnl, charges)`` for the caller's alert."""
    with locked(_STATE_FILE):
        state = _load()
        position = state.get("position") or {}
        entry = float(position.get("entry", 0.0))
        qty   = int(position.get("qty", 0))
        charges = round_trip_charges(entry, float(exit_price), qty)
        net = round(pnl - charges, 2)
        if state.get("day") != now.date().isoformat():
            state["day"] = now.date().isoformat()
            state["realized_pnl_today"] = 0.0
            state["stopped_out_today"] = False
        state["realized_pnl_today"] = round(state.get("realized_pnl_today", 0.0) + net, 2)
        state["last_exit_at"] = now.isoformat()
        if is_stop:
            state["stopped_out_today"] = True
        state.pop("position", None)
        _save(state)

    if position:
        ledger.log_trade(
            strategy="managed",
            ticker=ticker or os.getenv("TICKER", "EMCURE"),
            qty=qty,
            entry_price=entry,
            exit_price=float(exit_price),
            pnl=float(pnl),
            charges=charges,
            exit_reason="stop" if is_stop else "target",
            dry_run=not live,
            opened_at=position.get("opened_at"),
            closed_at=now,
        )
    return net, charges


def reentry_blocked(cfg: ManagedConfig, now: datetime) -> Optional[str]:
    """Reason a re-entry is currently blocked, or None if allowed. Enforces the
    HALT command, the daily-loss kill-switch, the same-day stop-out block, and
    the post-exit cooldown — so the cycle can't churn straight back in after a
    stop."""
    state = _load()
    if state.get("halted"):
        return "halted by HALT command (send RESUME to re-enable)"
    if state.get("day") != now.date().isoformat():
        return None  # new day — counters reset, nothing blocks
    if cfg.block_reentry_after_stop and state.get("stopped_out_today"):
        return "stopped out earlier today"
    if state.get("realized_pnl_today", 0.0) <= -cfg.max_daily_loss:
        return f"daily loss cap ₹{cfg.max_daily_loss:,.0f} reached"
    last = state.get("last_exit_at")
    if last:
        try:
            secs = (now - datetime.fromisoformat(last)).total_seconds()
            if 0 <= secs < cfg.reentry_cooldown_min * 60:
                mins_left = (cfg.reentry_cooldown_min * 60 - secs) / 60
                return f"re-entry cooldown ({mins_left:.0f} min left)"
        except ValueError:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Remote commands (written by bot_server, consumed by the tracker's step())
# ─────────────────────────────────────────────────────────────────────────────

def request_exit() -> Optional[dict]:
    """Ask the cycle to sell the open position on its next step (EXIT command).
    Returns the position that will be exited, or None when flat (no flag set)."""
    with locked(_STATE_FILE):
        state = _load()
        pos = state.get("position")
        if not pos:
            return None
        state["exit_requested"] = True
        _save(state)
        return pos


def _consume_exit_request() -> bool:
    """Pop the EXIT flag (if set) and report whether it was pending."""
    with locked(_STATE_FILE):
        state = _load()
        if state.pop("exit_requested", None):
            _save(state)
            return True
    return False


def set_halted(on: bool) -> None:
    """HALT/RESUME command: block all re-entries until explicitly resumed.
    Persists across restarts and day rolls — exits are never blocked."""
    def _apply(state: dict) -> None:
        if on:
            state["halted"] = True
        else:
            state.pop("halted", None)
    _mutate(_apply)


def is_halted() -> bool:
    return bool(_load().get("halted"))


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration (impure — broker calls + state writes + events)
# ─────────────────────────────────────────────────────────────────────────────

def _broker_avg_price(broker, ticker: str) -> float:
    """Average buy price of the live broker holding (delivery), or 0.0."""
    from src.execution.broker import _nse_symbol
    symbol = _nse_symbol(ticker)
    try:
        for h in broker.kite.holdings():
            if h.get("tradingsymbol") == symbol:
                return float(h.get("average_price") or 0.0)
        for p in broker.kite.positions().get("net", []):
            if p.get("tradingsymbol") == symbol and p.get("product") == "CNC":
                return float(p.get("average_price") or 0.0)
    except Exception:
        logger.exception("managed: could not read broker avg price for %s", symbol)
    return 0.0


def step(ticker: str, market: dict, broker, cfg: ManagedConfig,
         now: Optional[datetime] = None, df_daily=None) -> list[tuple[str, dict]]:
    """Run one managed-cycle step. Returns alert events (event_type, payload).

    Dry-run (cfg.live=False) announces what it WOULD do and places no orders.
    Hold/wait produce no event. Dry-run sell/buy/exit are de-duplicated per day
    so a held condition doesn't re-announce every cycle."""
    now = now or _now_ist()
    _maybe_roll_day(now)
    events: list[tuple[str, dict]] = []
    position = get_position()

    # Safety: if we think we hold but the broker shows zero (the resting stop
    # fired, or a manual sell), settle the record — booking a stop fill as such —
    # instead of trying to sell shares we no longer own. held_qty returns None on
    # a query error, so act only on a hard 0.
    if position and broker is not None and broker.held_qty(ticker) == 0:
        return _settle_closed_position(ticker, position, broker, cfg, now)

    # Adopt a broker holding the cycle isn't tracking yet (e.g. shares converted
    # to delivery by hand) so it manages them from the first cycle.
    if position is None and broker is not None:
        held = broker.held_qty(ticker)
        if held and held > 0:
            avg = _broker_avg_price(broker, ticker)
            if avg > 0:
                position = set_position(avg, held, cfg)
                events.append(("managed_adopt", {
                    "ticker": ticker, "entry": avg, "qty": held,
                    "sl": position["sl"], "targets": list(cfg.targets),
                }))
                logger.warning("Managed-cycle adopted holding: %d @ ₹%.2f", held, avg)
                if cfg.live:
                    _ensure_protective_stop(ticker, broker, cfg)   # resting exchange stop

    # Decision price: prefer the broker's real-time last-traded price over the
    # yfinance quote (NSE lags up to ~15 min there), so stop / target / re-entry
    # decisions act on live data. Only when authenticated; get_ltp returns 0.0 on
    # any failure, in which case we keep the caller's price. day_high / day_low
    # stay from the passed quote (kite.ltp carries no OHLC) — that's fine: the
    # held-position peak is tracked separately via high_since_entry below.
    if (broker is not None and getattr(broker, "is_authenticated", None)
            and broker.is_authenticated()):
        ltp = broker.get_ltp(ticker)
        if ltp and ltp > 0:
            market = {**market, "price": round(float(ltp), 2)}

    # Dynamic reach-probabilities from the CURRENT price (7/14/30-day moves) so the
    # target picker promotes higher targets only as they actually become likely.
    if position and df_daily is not None and "target_probs" not in market:
        from src.emcure.probability import daily_reach_probs
        up_levels = [round(float(position["entry"]) + d, 2) for d in cfg.targets]
        market = {**market, "target_probs": daily_reach_probs(df_daily, float(market.get("price", 0) or 0), up_levels)}

    # day_high / day_low from yfinance are SESSION-WIDE extremes, including the
    # pre-entry morning spike or crash — using them raw triggers immediate false
    # sells (high ≥ target) or false stop-outs (low ≤ sl) on the first cycle
    # after entry. Shadow both with the extremes actually observed WHILE holding:
    # high_since_entry / low_since_entry.
    if position:
        cur = float(market.get("price", 0) or 0)
        if cur > 0:
            prev_high = float(position.get("high_since_entry") or position["entry"])
            prev_low  = float(position.get("low_since_entry") or position["entry"])
            new_high  = max(prev_high, cur)
            new_low   = min(prev_low, cur)
            updates: dict[str, float] = {}
            if new_high != prev_high:
                updates["high_since_entry"] = round(new_high, 2)
            if new_low != prev_low:
                updates["low_since_entry"] = round(new_low, 2)
            if updates:
                _update_position(**updates)
                position = {**position, **updates}
            market = {**market, "day_high": new_high, "day_low": new_low}

    # A pending EXIT command overrides the normal decision: sell everything at
    # the current price. The flag is consumed either way — when flat there is
    # nothing to exit and the request simply expires.
    if _consume_exit_request() and position:
        decision = Decision(
            "sell", price=float(market.get("price", 0) or 0), qty=int(position["qty"]),
            reason="Manual EXIT command — selling at the current price",
        )
    else:
        decision = decide(position, market, cfg)
    logger.info("Managed-cycle decision: %s — %s", decision.action, decision.reason)

    if decision.action == "hold":
        if cfg.live and broker is not None:
            # Keep the stop resting while we hold — ratcheted to any touched floor.
            _ensure_protective_stop(ticker, broker, cfg)
        return events
    if decision.action == "wait":
        return events

    # Risk guards gate re-entries only — exits are always allowed.
    if decision.action == "reenter":
        blocked = reentry_blocked(cfg, now)
        if blocked:
            logger.info("Managed-cycle re-entry blocked — %s", blocked)
            if _set_once("last_block_sig", f"{blocked}:{now.date()}"):
                events.append(("managed_blocked", {"ticker": ticker, "reason": blocked}))
            return events

    if not cfg.live:
        # De-dup the dry-run announcement: only fire when the decision changes.
        if _set_once("last_dryrun_sig", f"{decision.action}:{decision.price}:{now.date()}"):
            events.append(("managed_dryrun", {
                "ticker": ticker, "decision": decision.action, "price": decision.price,
                "qty": decision.qty, "label": decision.label, "reason": decision.reason,
            }))
            logger.warning("Managed-cycle DRY-RUN: would %s — %s", decision.action, decision.reason)
        return events

    # ── LIVE execution (Phase 2) ─────────────────────────────────────────────
    # A live decision needs a broker to execute against. A None broker here means
    # the call was data-only (e.g. the pre-open briefing's _refresh) — never
    # fabricate a fill, emit a sell/buy event, or clear the position from it.
    if broker is None:
        logger.info("Managed-cycle: live decision '%s' skipped — no broker (data-only call)", decision.action)
        return events
    if decision.action in ("sell", "exit_sl"):
        return events + _execute_sell(ticker, decision, broker, now, cfg)
    if decision.action == "reenter":
        return events + _execute_buy(ticker, decision, broker, cfg)
    return events


def _settle_closed_position(ticker: str, position: dict, broker, cfg: ManagedConfig,
                            now: datetime) -> list[tuple[str, dict]]:
    """The broker shows 0 while we thought we held. If our resting stop filled,
    book it as a stop exit (with the real fill price + day tally); otherwise it
    was closed outside the bot. Either way the position record is cleared."""
    stop_id = position.get("stop_order_id")
    if cfg.live and stop_id and broker is not None:
        res = broker.order_result(stop_id)
        if res.get("status") == "COMPLETE" and res.get("filled_qty"):
            entry = float(position["entry"])
            qty   = int(res["filled_qty"])
            pnl   = int(round((res["fill_price"] - entry) * qty))
            net, charges = _record_exit(pnl, is_stop=True, now=now, ticker=ticker,
                                        exit_price=res["fill_price"], live=cfg.live)
            logger.warning("Managed-cycle STOP filled %d @ ₹%.2f  pnl=₹%d", qty, res["fill_price"], pnl)
            return [("managed_sell", {
                "ticker": ticker, "exit_price": res["fill_price"], "qty": qty,
                "entry": entry, "pnl": pnl, "net": net, "charges": charges,
                "label": "", "kind": "exit_sl",
                "reason": "Resting stop-loss filled at the exchange",
            })]
    clear_position()
    logger.warning("Managed-cycle: broker shows 0 — position closed externally, clearing")
    return [("managed_closed_externally", {"ticker": ticker, "qty": position.get("qty")})]


def _touched_floor(position: dict, cfg: ManagedConfig) -> Optional[float]:
    """Highest target rung the position has printed (via high_since_entry),
    or None when no rung has been touched yet."""
    entry = float(position["entry"])
    high = float(position.get("high_since_entry") or entry)
    touched = [t for t in (round(entry + d, 2) for d in cfg.targets) if high >= t]
    return max(touched) if touched else None


def _ensure_protective_stop(ticker: str, broker, cfg: Optional[ManagedConfig] = None) -> Optional[str]:
    """Guarantee a resting exchange stop exists for the open position — and
    RATCHET it up to the touched-target floor.

    Place a stop if missing, re-place if the recorded order was cancelled or
    rejected, and cancel/re-place at a higher trigger once a target rung has
    been touched. Without the ratchet the "never give a touched rung back"
    floor was enforced only by the 5-min polling loop: a flash drop between
    cycles (or with the bot offline) fell through to the original entry−SL
    stop, giving back the whole floor. The trigger only ever moves UP."""
    position = get_position()
    if not position or broker is None:
        return None

    desired = float(position["sl"])
    if cfg is not None:
        floor = _touched_floor(position, cfg)
        if floor is not None and floor > desired:
            desired = floor

    stop_id = position.get("stop_order_id")
    current = float(position.get("stop_trigger") or position["sl"])
    if stop_id:
        status = broker.order_state(stop_id)
        if status in ("CANCELLED", "REJECTED"):
            stop_id = None            # dead order — re-place below
        elif desired > current:       # ratchet: lift the resting trigger
            broker.cancel(stop_id)
            stop_id = None
            logger.warning("Ratcheting resting stop ₹%.2f → ₹%.2f", current, desired)
        else:
            return stop_id            # resting at the right level — leave it

    new_id = broker.place_stop_loss(ticker, int(position["qty"]), desired)
    _update_position(stop_order_id=new_id, stop_trigger=desired)
    return new_id


def _execute_sell(ticker: str, decision: Decision, broker, now: datetime,
                  cfg: ManagedConfig) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    pos = get_position() or {}
    # Cancel the resting stop first so it can't also fire and double-sell.
    stop_id = pos.get("stop_order_id")
    if stop_id and broker is not None:
        broker.cancel(stop_id)
    fill = broker.place_order_and_confirm(ticker, decision.qty, "SELL") if broker else None
    if broker and not fill:
        logger.error("Managed-cycle SELL did not fill — qty=%d", decision.qty)
        return [("managed_exit_failed", {
            "ticker": ticker, "qty": decision.qty, "reason": decision.reason,
        })]
    exit_price = fill["fill_price"] if fill else decision.price
    entry      = float(pos.get("entry", exit_price))
    pnl        = int(round((exit_price - entry) * decision.qty))
    # Books the NET P&L into the day's tally + sets the cooldown/stop-out flags so
    # the kill-switch can halt re-entries — and closes the position atomically.
    net, charges = _record_exit(pnl, is_stop=(decision.action == "exit_sl"), now=now,
                                ticker=ticker, exit_price=exit_price, live=cfg.live)
    logger.warning("Managed-cycle SOLD %d @ ₹%.2f  gross=₹%d net=₹%.0f",
                   decision.qty, exit_price, pnl, net)
    events.append(("managed_sell", {
        "ticker": ticker, "exit_price": exit_price, "qty": decision.qty,
        "entry": entry, "pnl": pnl, "net": net, "charges": charges,
        "label": decision.label, "kind": decision.action, "reason": decision.reason,
    }))
    return events


def _execute_buy(ticker: str, decision: Decision, broker, cfg: ManagedConfig) -> list[tuple[str, dict]]:
    if broker:
        held = broker.held_qty(ticker)
        if held:
            logger.error("Managed-cycle BUY skipped — broker already holds %d", held)
            return [("managed_reconcile_warn", {"ticker": ticker, "held": held})]
        funds = broker.available_funds()
        need  = decision.price * decision.qty
        if funds is not None and funds < need:
            return [("managed_insufficient_funds", {
                "ticker": ticker, "need": need, "have": funds, "qty": decision.qty,
            })]
    fill = broker.place_order_and_confirm(ticker, decision.qty, "BUY") if broker else None
    if broker and not fill:
        return [("managed_open_failed", {"ticker": ticker, "qty": decision.qty})]
    entry = fill["fill_price"] if fill else decision.price
    qty   = fill["filled_qty"] if fill else decision.qty
    pos   = set_position(entry, qty, cfg)
    if broker is not None:
        _update_position(stop_order_id=broker.place_stop_loss(ticker, qty, pos["sl"]),
                         stop_trigger=pos["sl"])
    logger.warning("Managed-cycle BOUGHT %d @ ₹%.2f", qty, entry)
    return [("managed_buy", {
        "ticker": ticker, "entry": entry, "qty": qty, "sl": pos["sl"],
        "targets": list(cfg.targets), "reason": decision.reason,
    })]


# ─────────────────────────────────────────────────────────────────────────────
# Alert formatting
# ─────────────────────────────────────────────────────────────────────────────

def format_levels_block(cfg: ManagedConfig, position: Optional[dict],
                        sma7: float, probs: Optional[dict] = None) -> str:
    """Managed-cycle levels for the scheduled briefings — the target ladder + stop
    from the booked entry when holding, or the SMA7 re-entry trigger when flat.
    Replaces the legacy +₹10/20/25 probability ladder so every number a briefing
    shows matches what the cycle will actually trade.

    `probs` (from probability.daily_reach_probs, keyed by absolute target price
    plus "stop") shows each level's dynamic reach-odds AND drives the chosen
    target shown."""
    test = "  (test mode)" if not cfg.live else ""
    if position:
        entry = float(position["entry"])
        qty   = int(position["qty"])
        sl    = float(position["sl"])
        top   = round(entry + max(cfg.targets), 2)
        target_price, aim = top, ""
        if probs:
            chosen = choose_target(entry, probs, cfg)
            if chosen:
                target_price = chosen["price"]
                aim = f"  ({chosen['prob']}% chance today)"
        return "\n".join([
            f"📊 *Holding {qty} shares — bought ₹{entry:,.2f}*{test}",
            f"Aiming for ₹{target_price:,.2f}{aim}",
            f"Will exit to protect if it drops to ₹{sl:,.2f}",
        ])

    # Flat — waiting to buy the dip. Mirror decide()'s threshold exactly so the
    # briefing's trigger price matches what the cycle will actually act on.
    gap_rupees = cfg.reentry_gap
    if cfg.reentry_gap_pct > 0 and float(sma7) > 0:
        gap_rupees = round(float(sma7) * cfg.reentry_gap_pct / 100, 2)
    reentry = round(float(sma7) - gap_rupees, 2)
    top     = round(reentry + max(cfg.targets), 2)
    return "\n".join([
        f"📊 *No shares right now — watching to buy*{test}",
        f"Will buy {cfg.qty} shares if the price dips to about ₹{reentry:,.2f}",
        f"Then aim for ₹{top:,.2f}, exiting to protect near ₹{reentry - cfg.sl_rupees:,.2f}",
    ])


def levels_block_from(data: dict) -> Optional[str]:
    """Build the briefing levels block straight from a ``_refresh()`` data dict,
    or None when the managed cycle is disabled. Shared by apps/main.py and
    apps/main_headless.py so the two entry points render identical numbers."""
    cfg = ManagedConfig.from_env()
    if not cfg.enabled:
        return None
    sma7 = float((data.get("sma7_gap") or {}).get("sma7", 0) or 0)
    return format_levels_block(cfg, get_position(), sma7, data.get("managed_probs"))


def format_managed_event(ticker: str, event_type: str, p: dict) -> Optional[str]:
    """WhatsApp/Telegram message for a managed-cycle event, or None to skip."""
    if event_type == "managed_adopt":
        top = round(float(p["entry"]) + max(p["targets"]), 2)
        return (
            f"📋 *Now managing {ticker}*\n"
            f"Watching your {p['qty']} shares (bought around ₹{p['entry']:,.2f})\n"
            f"Aiming for ₹{top:,.2f}  ·  safety exit ₹{p['sl']:,.2f}"
        )
    if event_type == "managed_dryrun":
        verb = {"sell": "sell", "exit_sl": "sell (safety exit)", "reenter": "buy"}.get(p["decision"], p["decision"])
        return (
            f"🧪 *Test mode — {ticker}*\n"
            f"Would {verb} {p['qty']} shares at ₹{p['price']:,.2f}\n"
            f"{p['reason']}\n"
            f"_Test only — no real order placed._"
        )
    if event_type == "managed_sell":
        # Net of charges when the exit recorded them — the number that actually
        # lands in the account, not the flattering gross.
        pnl_net = p.get("net", p["pnl"])
        won  = pnl_net >= 0
        head = "🛑 *Sold" if p["kind"] == "exit_sl" else ("✅ *Sold" if won else "🔴 *Sold")
        money = f"Profit ₹{pnl_net:,.0f}" if won else f"Loss ₹{abs(pnl_net):,.0f}"
        charge_note = f" after ₹{p['charges']:,.0f} charges" if p.get("charges") else ""
        return (
            f"{head} {ticker}*\n"
            f"{p['qty']} shares at ₹{p['exit_price']:,.2f}\n"
            f"{money}{charge_note}  (bought at ₹{p['entry']:,.2f})\n"
            f"{p['reason']}"
        )
    if event_type == "managed_buy":
        first = round(float(p["entry"]) + min(p["targets"]), 2)
        top   = round(float(p["entry"]) + max(p["targets"]), 2)
        return (
            f"🟢 *Bought {ticker}*\n"
            f"{p['qty']} shares at ₹{p['entry']:,.2f}\n"
            f"Target ₹{first:,.2f}–₹{top:,.2f}  ·  safety exit ₹{p['sl']:,.2f}\n"
            f"{p['reason']}"
        )
    if event_type == "managed_exit_failed":
        return (
            f"🚨 *Sell didn't go through — {ticker}*\n"
            f"Tried to sell {p['qty']} shares but the order didn't complete. "
            f"You may still own them — please check Zerodha now."
        )
    if event_type == "managed_open_failed":
        return (
            f"⚠️ *Buy didn't go through — {ticker}*\n"
            f"Tried to buy {p['qty']} shares but the order didn't complete. Nothing was bought."
        )
    if event_type == "managed_reconcile_warn":
        return (
            f"⚠️ *Buy skipped — {ticker}*\n"
            f"Wanted to buy, but Zerodha already shows {p['held']} shares held. "
            f"Nothing bought — please check your positions."
        )
    if event_type == "managed_insufficient_funds":
        return (
            f"💸 *Buy skipped — {ticker}*\n"
            f"Need ₹{p['need']:,.0f} to buy {p['qty']} shares, but only ₹{p['have']:,.0f} is available."
        )
    if event_type == "managed_blocked":
        return (
            f"⏸️ *Holding off — {ticker}*\n"
            f"Wanted to buy again, but paused: {p['reason']}.\n"
            f"No new buy today unless this clears."
        )
    if event_type == "managed_closed_externally":
        return (
            f"ℹ️ *Position cleared — {ticker}*\n"
            f"Zerodha shows 0 shares (sold outside the app), so I've cleared the tracked "
            f"{p.get('qty', '?')} shares. I'll look for a fresh entry."
        )
    return None
