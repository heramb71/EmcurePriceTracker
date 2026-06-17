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

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    reach_atr_factor: float       # target reachable if delta <= atr × this
    max_daily_loss: float         # block new entries once realized day loss ≥ this (₹)
    reentry_cooldown_min: float   # min minutes between an exit and the next entry
    block_reentry_after_stop: bool  # no re-entry the same day as a stop-out

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
            reach_atr_factor = float(os.getenv("MANAGED_REACH_ATR_FACTOR", "1.0")),
            # Default the daily-loss cap to one full stop (sl × qty): after one
            # stop-out the realized loss hits the cap and re-entries halt for the day.
            max_daily_loss   = float(os.getenv("MANAGED_MAX_DAILY_LOSS", str(sl_rupees * qty))),
            reentry_cooldown_min     = float(os.getenv("MANAGED_REENTRY_COOLDOWN_MIN", "60")),
            block_reentry_after_stop = os.getenv("MANAGED_BLOCK_REENTRY_AFTER_STOP", "true").lower() == "true",
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

def choose_target(entry: float, atr: float, cfg: ManagedConfig) -> Optional[dict]:
    """Highest target reachable today, judged by the day's volatility (ATR).

    A target at entry+delta is 'reachable' if delta <= atr × reach_atr_factor —
    i.e. a normal day's range plausibly covers the move. Returns the highest
    reachable target, or None on a day too quiet to aim for even the smallest
    delta (caller then just holds for the stop or a livelier day)."""
    if atr <= 0:
        return None
    headroom = atr * cfg.reach_atr_factor
    reachable = [d for d in cfg.targets if d <= headroom]
    if not reachable:
        return None
    delta = max(reachable)
    return {"delta": delta, "price": round(entry + delta, 2), "label": f"+₹{delta:.0f}"}


def decide(position: Optional[dict], market: dict, cfg: ManagedConfig) -> Decision:
    """One cycle's decision. position carries entry/qty/sl (None = flat); market
    carries price/day_high/day_low/atr/gap/trend_7d."""
    price = float(market.get("price", 0) or 0)
    high  = float(market.get("day_high", 0) or 0)
    low   = float(market.get("day_low", 0) or 0)
    atr   = float(market.get("atr", 0) or 0)

    if position:
        entry = float(position["entry"])
        qty   = int(position["qty"])
        sl    = float(position["sl"])

        # 1. Capital protection first — stop hit on the day's low or live price.
        if (low and low <= sl) or (price and price <= sl):
            return Decision("exit_sl", reason=f"Stop ₹{sl:,.2f} hit", price=sl, qty=qty)

        # 2. Sell at the highest target the day can plausibly reach.
        chosen = choose_target(entry, atr, cfg)
        if chosen and ((high and high >= chosen["price"]) or price >= chosen["price"]):
            return Decision(
                "sell", reason=f"Reached {chosen['label']} target ₹{chosen['price']:,.2f}",
                price=chosen["price"], qty=qty, label=chosen["label"],
            )

        # 3. Hold, waiting for the chosen target or the stop.
        tgt   = chosen["price"] if chosen else 0.0
        label = chosen["label"] if chosen else "no target reachable today"
        detail = f" ₹{tgt:,.2f}" if tgt else ""
        return Decision("hold", reason=f"Holding for {label}{detail}", price=tgt, qty=qty, label=label)

    # Flat → SMA7 mean-reversion re-entry.
    gap   = float(market.get("gap", 0) or 0)          # price − sma7 (negative = below)
    trend = market.get("trend_7d", "")
    if gap <= -cfg.reentry_gap and trend != "Downward":
        return Decision(
            "reenter", reason=f"Price ₹{abs(gap):.0f} below 7-day SMA — mean-reversion entry",
            price=price, qty=cfg.qty,
        )
    downtrend = " (downtrend — skip)" if trend == "Downward" else ""
    return Decision("wait", reason=f"No entry — gap ₹{gap:+.0f} vs SMA7{downtrend}")


# ─────────────────────────────────────────────────────────────────────────────
# State I/O (own file — never shares with strategy_state.json)
# ─────────────────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_position() -> Optional[dict]:
    return _load().get("position")


def _update_position(**fields) -> None:
    """Merge fields into the stored position (e.g. the resting stop's order id)."""
    state = _load()
    pos = state.get("position")
    if pos:
        pos.update(fields)
        state["position"] = pos
        _save(state)


def set_position(entry: float, qty: int, cfg: ManagedConfig) -> dict:
    pos = {
        "entry":     round(float(entry), 2),
        "qty":       int(qty),
        "sl":        round(float(entry) - cfg.sl_rupees, 2),
        "targets":   list(cfg.targets),
        "opened_at": datetime.now().isoformat(timespec="seconds"),
    }
    state = _load()
    state["position"] = pos
    _save(state)
    return pos


def clear_position() -> None:
    state = _load()
    state.pop("position", None)
    _save(state)


# ─────────────────────────────────────────────────────────────────────────────
# Daily risk guards (kill-switch + re-entry cooldown)
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_roll_day(now: datetime) -> None:
    """Reset the day's realized-loss tally and stop-out flag at a date change."""
    today = now.date().isoformat()
    state = _load()
    if state.get("day") != today:
        state["day"] = today
        state["realized_pnl_today"] = 0.0
        state["stopped_out_today"] = False
        _save(state)


def _record_exit(pnl: float, is_stop: bool, now: datetime) -> None:
    """Atomically close the position and book the exit into the day's tally so the
    kill-switch and cooldown can see it."""
    state = _load()
    if state.get("day") != now.date().isoformat():
        state["day"] = now.date().isoformat()
        state["realized_pnl_today"] = 0.0
        state["stopped_out_today"] = False
    state["realized_pnl_today"] = round(state.get("realized_pnl_today", 0.0) + pnl, 2)
    state["last_exit_at"] = now.isoformat()
    if is_stop:
        state["stopped_out_today"] = True
    state.pop("position", None)
    _save(state)


def reentry_blocked(cfg: ManagedConfig, now: datetime) -> Optional[str]:
    """Reason a re-entry is currently blocked, or None if allowed. Enforces the
    daily-loss kill-switch, the same-day stop-out block, and the post-exit
    cooldown — so the cycle can't churn straight back in after a stop."""
    state = _load()
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
# Orchestration (impure — broker calls + state writes + events)
# ─────────────────────────────────────────────────────────────────────────────

def _broker_avg_price(broker, ticker: str) -> float:
    """Average buy price of the live broker holding (delivery), or 0.0."""
    from src.broker import _nse_symbol
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
         now: Optional[datetime] = None) -> list[tuple[str, dict]]:
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
                    _ensure_protective_stop(ticker, broker)   # resting exchange stop

    decision = decide(position, market, cfg)
    logger.info("Managed-cycle decision: %s — %s", decision.action, decision.reason)

    if decision.action == "hold":
        if cfg.live and broker is not None:
            _ensure_protective_stop(ticker, broker)   # keep the stop resting while we hold
        return events
    if decision.action == "wait":
        return events

    # Risk guards gate re-entries only — exits are always allowed.
    if decision.action == "reenter":
        blocked = reentry_blocked(cfg, now)
        if blocked:
            logger.info("Managed-cycle re-entry blocked — %s", blocked)
            state = _load()
            sig   = f"{blocked}:{now.date()}"
            if state.get("last_block_sig") != sig:
                state["last_block_sig"] = sig
                _save(state)
                events.append(("managed_blocked", {"ticker": ticker, "reason": blocked}))
            return events

    if not cfg.live:
        # De-dup the dry-run announcement: only fire when the decision changes.
        sig   = f"{decision.action}:{decision.price}:{now.date()}"
        state = _load()
        if state.get("last_dryrun_sig") != sig:
            state["last_dryrun_sig"] = sig
            _save(state)
            events.append(("managed_dryrun", {
                "ticker": ticker, "decision": decision.action, "price": decision.price,
                "qty": decision.qty, "label": decision.label, "reason": decision.reason,
            }))
            logger.warning("Managed-cycle DRY-RUN: would %s — %s", decision.action, decision.reason)
        return events

    # ── LIVE execution (Phase 2) ─────────────────────────────────────────────
    if decision.action in ("sell", "exit_sl"):
        return events + _execute_sell(ticker, decision, broker, now)
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
            _record_exit(pnl, is_stop=True, now=now)
            logger.warning("Managed-cycle STOP filled %d @ ₹%.2f  pnl=₹%d", qty, res["fill_price"], pnl)
            return [("managed_sell", {
                "ticker": ticker, "exit_price": res["fill_price"], "qty": qty,
                "entry": entry, "pnl": pnl, "label": "", "kind": "exit_sl",
                "reason": "Resting stop-loss filled at the exchange",
            })]
    clear_position()
    logger.warning("Managed-cycle: broker shows 0 — position closed externally, clearing")
    return [("managed_closed_externally", {"ticker": ticker, "qty": position.get("qty")})]


def _ensure_protective_stop(ticker: str, broker) -> Optional[str]:
    """Guarantee a resting exchange stop exists for the open position: place one
    if missing, or re-place if the recorded order was cancelled/rejected. So the
    exchange enforces the stop even if the bot is offline between cycles."""
    position = get_position()
    if not position or broker is None:
        return None
    stop_id = position.get("stop_order_id")
    if stop_id:
        status = broker.order_state(stop_id)
        if status not in ("CANCELLED", "REJECTED"):
            return stop_id            # resting / complete / unknown — leave it
    new_id = broker.place_stop_loss(ticker, int(position["qty"]), float(position["sl"]))
    _update_position(stop_order_id=new_id)
    return new_id


def _execute_sell(ticker: str, decision: Decision, broker, now: datetime) -> list[tuple[str, dict]]:
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
    # Books the P&L into the day's tally + sets the cooldown/stop-out flags so the
    # kill-switch can halt re-entries — and closes the position atomically.
    _record_exit(pnl, is_stop=(decision.action == "exit_sl"), now=now)
    logger.warning("Managed-cycle SOLD %d @ ₹%.2f  pnl=₹%d", decision.qty, exit_price, pnl)
    events.append(("managed_sell", {
        "ticker": ticker, "exit_price": exit_price, "qty": decision.qty,
        "entry": entry, "pnl": pnl, "label": decision.label,
        "kind": decision.action, "reason": decision.reason,
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
        _update_position(stop_order_id=broker.place_stop_loss(ticker, qty, pos["sl"]))
    logger.warning("Managed-cycle BOUGHT %d @ ₹%.2f", qty, entry)
    return [("managed_buy", {
        "ticker": ticker, "entry": entry, "qty": qty, "sl": pos["sl"],
        "targets": list(cfg.targets), "reason": decision.reason,
    })]


# ─────────────────────────────────────────────────────────────────────────────
# Alert formatting
# ─────────────────────────────────────────────────────────────────────────────

def format_levels_block(cfg: ManagedConfig, position: Optional[dict],
                        sma7: float, atr: float, probs: Optional[dict] = None) -> str:
    """Managed-cycle levels for the scheduled briefings — the target ladder + stop
    from the booked entry when holding, or the SMA7 re-entry trigger when flat.
    Replaces the legacy +₹10/20/25 probability ladder so every number a briefing
    shows matches what the cycle will actually trade.

    `probs` (from probability.touch_probabilities, keyed by absolute level price
    plus "stop") appends each level's empirical touch-odds when supplied."""
    mode = "live" if cfg.live else "dry-run"
    if position:
        entry = float(position["entry"])
        qty   = int(position["qty"])
        sl    = float(position["sl"])
        lines = [f"🎯 *Managed plan — holding {qty} sh @ ₹{entry:,.2f}*  ({mode})"]
        for i, d in enumerate(cfg.targets):
            lvl = round(entry + d, 2)
            p   = (probs or {}).get(lvl)
            odds = f"  ·  {p}%" if p is not None else ""
            lines.append(f"T{i + 1}  ₹{lvl:,.2f}  (+₹{d:.0f} · ₹{d * qty:,.0f}){odds}")
        sp   = (probs or {}).get("stop")
        sodds = f"  ·  {sp}%" if sp is not None else ""
        lines.append(f"Stop  ₹{sl:,.2f}  (−₹{cfg.sl_rupees:.0f} · ₹{cfg.sl_rupees * qty:,.0f}){sodds}")
        if probs:
            lines.append("_odds = chance of touching within ~5 trading days (from history)_")
        chosen = choose_target(entry, atr, cfg)
        if chosen:
            lines.append(f"Today's range favours exiting at {chosen['label']} → ₹{chosen['price']:,.2f}")
        else:
            lines.append("Today's range looks too quiet to reach a target — holding for the stop or a livelier day.")
        return "\n".join(lines)

    # Flat — waiting to re-enter.
    reentry = round(float(sma7) - cfg.reentry_gap, 2)
    ladder  = "/".join(f"+₹{d:.0f}" for d in cfg.targets)
    return "\n".join([
        f"🎯 *Managed plan — flat, watching to re-enter*  ({mode})",
        f"Re-enter when price ≤ ₹{reentry:,.2f}  (₹{cfg.reentry_gap:.0f} below the 7-day avg)",
        f"Then {cfg.qty} sh, targets {ladder} from entry, stop −₹{cfg.sl_rupees:.0f}",
    ])


def format_managed_event(ticker: str, event_type: str, p: dict) -> Optional[str]:
    """WhatsApp/Telegram message for a managed-cycle event, or None to skip."""
    if event_type == "managed_adopt":
        tlist = "  ".join(f"+₹{d:.0f}" for d in p["targets"])
        return (
            f"📋 *Managed cycle tracking — {ticker}*\n\n"
            f"Now managing {p['qty']} sh @ ₹{p['entry']:,.2f}\n"
            f"Targets: {tlist}    Stop: ₹{p['sl']:,.2f}"
        )
    if event_type == "managed_dryrun":
        verb = {"sell": "SELL", "exit_sl": "STOP-OUT (sell)", "reenter": "BUY"}.get(p["decision"], p["decision"].upper())
        return (
            f"🧪 *Managed cycle (dry-run) — {ticker}*\n\n"
            f"WOULD {verb} {p['qty']} sh @ ₹{p['price']:,.2f}"
            + (f"  ({p['label']})" if p.get("label") else "") + "\n"
            f"{p['reason']}\n\n"
            f"_No real order placed. Set MANAGED_CYCLE_LIVE=true to go live._"
        )
    if event_type == "managed_sell":
        sign = "+" if p["pnl"] >= 0 else ""
        head = "🛑 *Stop-loss exit*" if p["kind"] == "exit_sl" else "🎯 *Target hit — sold*"
        return (
            f"{head} — {ticker}\n\n"
            f"Sold {p['qty']} sh @ ₹{p['exit_price']:,.2f}  (entry ₹{p['entry']:,.2f})\n"
            f"P&L: {sign}₹{p['pnl']:,.0f}\n{p['reason']}"
        )
    if event_type == "managed_buy":
        tlist = "  ".join(f"+₹{d:.0f}" for d in p["targets"])
        return (
            f"🟢 *Managed cycle — bought {ticker}*\n\n"
            f"{p['qty']} sh @ ₹{p['entry']:,.2f}\n"
            f"Targets: {tlist}    Stop: ₹{p['sl']:,.2f}\n{p['reason']}"
        )
    if event_type == "managed_exit_failed":
        return (
            f"🚨 *Managed SELL FAILED — {ticker}*\n\n"
            f"Tried to sell {p['qty']} sh ({p['reason']}) but the order did not fill. "
            f"Your position may still be OPEN — check Zerodha now."
        )
    if event_type == "managed_open_failed":
        return f"⚠️ *Managed BUY not filled — {ticker}*\n\nTried to buy {p['qty']} sh; order did not fill. No position opened."
    if event_type == "managed_reconcile_warn":
        return (
            f"⚠️ *Managed BUY skipped — {ticker}*\n\n"
            f"A re-entry fired but Zerodha already shows {p['held']} sh held. No order placed — check positions."
        )
    if event_type == "managed_insufficient_funds":
        return (
            f"💸 *Managed BUY skipped — {ticker}*\n\n"
            f"Re-entry needs ₹{p['need']:,.0f} for {p['qty']} sh but only ₹{p['have']:,.0f} is available."
        )
    if event_type == "managed_blocked":
        return (
            f"⏸️ *Managed re-entry paused — {ticker}*\n\n"
            f"A re-entry signalled but is blocked: {p['reason']}.\n"
            f"No new position today unless this clears."
        )
    if event_type == "managed_closed_externally":
        return (
            f"ℹ️ *Managed position cleared — {ticker}*\n\n"
            f"Zerodha shows 0 shares (sold outside the bot), so the tracked "
            f"{p.get('qty', '?')}-share position was cleared. The cycle will look for a fresh entry."
        )
    return None
