"""The daily-flow orchestrator — wires the pure modules to data, broker, state.

State machine per trading day (driven by :meth:`KittyBotEngine.step`, called each
loop tick with the current IST time):

    09:15  prepare()  — load kitty, staleness/halt rails, earnings+gap discards
    09:30+ try_enter() — VIX rail, build opening ranges, pick strongest breakout,
                         size to ≤1% risk, place ONE entry (paper unless live)
    …      manage()    — ratchet stop to breakeven at +1%, exit on target/stop
    15:10  manage()    — hard time-exit regardless of P&L

Only one position per day; no re-entry after it closes (spec). Everything the bot
decides — including every skip — is written to the journal. The risk-bearing
choices all live in the pure modules this class calls; the class itself only
sequences them and performs I/O.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from src.kittybot import journal, marketdata, safety, state
from src.kittybot.broker import BUY, SELL, Broker, make_broker
from src.kittybot.config import KittyBotConfig
from src.kittybot.filters import apply_filters
from src.kittybot.opening_range import LONG, breakout_trigger
from src.kittybot.picks import Pick, load_kitty
from src.kittybot.risk import TradePlan, breakeven_stop, exit_reason, plan_trade, realized_pnl
from src.kittybot.selection import select_trigger

logger = logging.getLogger("kittybot.engine")

_PLAN_FIELDS = ("symbol", "direction", "entry", "stop", "target", "qty", "risk_rupees")


class KittyBotEngine:
    """Sequences one trading day. Cross-day state persists via :mod:`state`."""

    def __init__(self, cfg: KittyBotConfig, broker: Broker | None = None, notifier=None):
        self.cfg = cfg
        self.broker = broker or make_broker(
            cfg, price_fn=lambda s: (marketdata.live_tick(s) or (None,))[0]
        )
        self.notifier = notifier  # optional; Telegram alerts, None → journal-only
        self._session: date | None = None
        self._prepared = False
        self._skip_day = False
        self._entered = False       # one entry per day; no re-entry after close
        self._vix_checked = False
        self._survivors: list[Pick] = []

    # ── session reset ─────────────────────────────────────────────────────────
    def _ensure_session(self, today: date) -> None:
        if self._session == today:
            return
        self._session = today
        self._prepared = False
        self._skip_day = False
        self._entered = False
        self._vix_checked = False
        self._survivors = []

    # ── public tick ───────────────────────────────────────────────────────────
    def step(self, now: datetime) -> None:
        """Advance the state machine for the current IST wall-clock ``now``."""
        today = now.date()
        self._ensure_session(today)

        # Manage an open position first — the hard time-exit must always run.
        if state.get_position(self.cfg.state_path):
            self._manage(now)
            return

        if not self._prepared and now.time() >= self.cfg.observe_start_t:
            self._prepare(now)

        if self._ready_to_enter(now):
            self._try_enter(now)

    def _ready_to_enter(self, now: datetime) -> bool:
        return (
            self._prepared
            and not self._skip_day
            and not self._entered
            and self.cfg.select_time_t <= now.time() < self.cfg.no_trade_after_t
        )

    # ── step 1: prepare (load kitty, rails, discards) ─────────────────────────
    def _prepare(self, now: datetime) -> None:
        self._prepared = True
        journal.record(self.cfg.journal_dir, journal.START,
                       {"session": now.date().isoformat(), "broker": self.broker.name,
                        "live": self.cfg.sends_real_orders}, when=now)

        kitty = load_kitty(self.cfg)
        halt_until = state.parse_halt_until(state.load(self.cfg.state_path))
        decision = safety.evaluate(
            vix_now=None, vix_prev_close=None, vix_spike_pct=self.cfg.vix_spike_pct,
            generated_at=kitty.generated_at, now=now,
            picks_max_age_hours=self.cfg.picks_max_age_hours, halt_until=halt_until,
        )
        # VIX is checked separately at select time; here only staleness + halt block.
        blocking = [c for c in decision.checks if c.blocked and c.name != "India VIX spike"]
        if blocking:
            self._skip_day = True
            reasons = [f"{c.name}: {c.detail}" for c in blocking]
            journal.record(self.cfg.journal_dir, journal.SKIP_DAY, {"reasons": reasons}, when=now)
            if self.notifier:
                self.notifier.skip(reasons)
            return

        quotes = {p.symbol: marketdata.opening_quote(p.symbol) for p in kitty.picks}
        quotes = {k: v for k, v in quotes.items() if v is not None}
        kept, discarded = apply_filters(kitty.picks, quotes, now.date(), self.cfg.gap_max_pct)
        for pick, reason in discarded:
            journal.record(self.cfg.journal_dir, journal.DISCARD,
                           {"symbol": pick.symbol, "reason": reason}, when=now)
        self._survivors = kept
        journal.record(self.cfg.journal_dir, journal.OBSERVE,
                       {"source": kitty.source, "survivors": [p.symbol for p in kept]}, when=now)
        if self.notifier:
            self.notifier.daily_plan(kept, kitty.source)

    # ── step 2/3: observe opening range, select, enter ────────────────────────
    def _check_vix(self, now: datetime) -> bool:
        """Return True if the day should be skipped for a VIX spike (checked once)."""
        if self._vix_checked:
            return self._skip_day
        self._vix_checked = True
        vix_now, vix_prev = marketdata.india_vix()
        if safety.vix_spike(vix_now, vix_prev, self.cfg.vix_spike_pct):
            self._skip_day = True
            reasons = [f"India VIX spike: {vix_now} vs {vix_prev}"]
            journal.record(self.cfg.journal_dir, journal.SKIP_DAY, {"reasons": reasons}, when=now)
            if self.notifier:
                self.notifier.skip(reasons)
        return self._skip_day

    def _try_enter(self, now: datetime) -> None:
        if self._check_vix(now):
            return
        if not self._survivors:
            return

        triggers = []
        by_symbol: dict[str, Pick] = {}
        for pick in self._survivors:
            or_range = marketdata.opening_range(pick.symbol, self.cfg.opening_range_minutes)
            tick = marketdata.live_tick(pick.symbol)
            if or_range is None or tick is None:
                continue
            price, vol = tick
            trig = breakout_trigger(pick, or_range, price, vol, self.cfg.breakout_volume_multiple)
            if trig is not None:
                triggers.append(trig)
                by_symbol[pick.symbol] = pick

        winner = select_trigger(triggers)
        if winner is None:
            # No trigger yet — the loop retries until no_trade_after, then logs once.
            if now.time() >= self.cfg.no_trade_after_t:
                journal.record(self.cfg.journal_dir, journal.NO_TRIGGER,
                               {"note": "no breakout by cutoff — no trade today"}, when=now)
                if self.notifier:
                    self.notifier.skip(["no opening-range breakout by cutoff"])
                self._entered = True  # stop trying for the day
            return

        self._place_entry(winner, by_symbol[winner.symbol], now)

    def _place_entry(self, winner, pick: Pick, now: datetime) -> None:
        self._entered = True  # commit: one entry per day, win or lose
        plan = plan_trade(
            symbol=winner.symbol, direction=winner.direction, entry=winner.trigger_price,
            target_pct=pick.suggested_target_pct, stop_pct=pick.suggested_stop_pct,
            capital=self.cfg.capital, risk_pct=self.cfg.risk_per_trade_pct,
        )
        if plan is None:
            journal.record(self.cfg.journal_dir, journal.ENTRY_FAILED,
                           {"symbol": winner.symbol, "reason": "position size 0 (stop too wide)"},
                           when=now)
            return
        journal.record(self.cfg.journal_dir, journal.SELECT,
                       {"symbol": plan.symbol, "direction": plan.direction,
                        "strength": round(winner.strength, 4), "entry": plan.entry,
                        "target": plan.target, "stop": plan.stop, "qty": plan.qty,
                        "risk_rupees": plan.risk_rupees}, when=now)

        side = BUY if plan.direction == LONG else SELL
        fill = self.broker.place_market(plan.symbol, plan.qty, side, self.cfg.product)
        if fill is None or fill.status != "COMPLETE":
            journal.record(self.cfg.journal_dir, journal.ENTRY_FAILED,
                           {"symbol": plan.symbol, "reason": "broker did not fill entry"}, when=now)
            return
        state.open_position(self.cfg.state_path, plan, session_date=now.date(),
                            entry_order_id=fill.order_id, fill_price=fill.price)
        journal.record(self.cfg.journal_dir, journal.ENTRY,
                       {"symbol": plan.symbol, "side": side, "qty": fill.qty,
                        "fill": fill.price, "order_id": fill.order_id}, when=now)
        if self.notifier:
            self.notifier.entry(plan, fill.price)

    # ── manage the open position ──────────────────────────────────────────────
    @staticmethod
    def _plan_from_position(pos: dict) -> TradePlan:
        return TradePlan(**{k: pos[k] for k in _PLAN_FIELDS})

    def _manage(self, now: datetime) -> None:
        pos = state.get_position(self.cfg.state_path)
        if not pos:
            return
        tick = marketdata.live_tick(pos["symbol"])
        # Even with no fresh tick we must honour the hard time-exit.
        price = tick[0] if tick else pos.get("entry_fill", pos["entry"])
        plan = self._plan_from_position(pos)
        live_stop = pos.get("live_stop", plan.stop)

        new_stop = breakeven_stop(plan.entry, plan.direction, price, live_stop,
                                  self.cfg.breakeven_trigger_pct)
        if new_stop != live_stop:
            state.update_stop(self.cfg.state_path, new_stop)
            live_stop = new_stop
            journal.record(self.cfg.journal_dir, journal.STOP_MOVED,
                           {"symbol": plan.symbol, "stop": new_stop, "reason": "breakeven +1%"},
                           when=now)
            if self.notifier:
                self.notifier.breakeven(plan.symbol, new_stop)

        reason = exit_reason(plan, price, now.time(), self.cfg.hard_exit_t, stop=live_stop)
        if reason is not None:
            self._exit(plan, price, reason, now)

    def _exit(self, plan: TradePlan, price: float, reason: str, now: datetime) -> None:
        side = SELL if plan.direction == LONG else BUY
        fill = self.broker.place_market(plan.symbol, plan.qty, side, self.cfg.product)
        exit_price = fill.price if fill and fill.status == "COMPLETE" else price
        pnl = realized_pnl(plan, exit_price)
        is_loss = pnl < 0
        state.close_position(self.cfg.state_path, result_date=now.date(), is_loss=is_loss)
        self._maybe_halt(now)
        journal.record(self.cfg.journal_dir, journal.EXIT,
                       {"symbol": plan.symbol, "reason": reason, "exit": exit_price,
                        "pnl": pnl, "filled": bool(fill and fill.status == "COMPLETE")}, when=now)
        if self.notifier:
            self.notifier.exit(plan.symbol, reason, exit_price, pnl)

    def _maybe_halt(self, now: datetime) -> None:
        st = state.load(self.cfg.state_path)
        if safety.loss_streak_halt(st.get("loss_streak", 0), self.cfg.max_consecutive_losing_days):
            resume = safety.resume_date(now.date())
            state.set_halt(self.cfg.state_path, resume)
            journal.record(self.cfg.journal_dir, journal.SKIP_DAY,
                           {"reasons": [f"loss streak {st['loss_streak']} — halted until {resume}"]},
                           when=now)
