#!/usr/bin/env python3
"""
Manual trade CLI.

Usage:
  python trade.py buy 1693          # record entry at ₹1693 (qty auto from CAPITAL)
  python trade.py buy 1693 60       # record entry at ₹1693, qty 60
  python trade.py sell              # close the trade
  python trade.py status            # show live P&L
  python trade.py holding           # read-only: show live Zerodha delivery
                                    # holding (qty + avg buy price) + levels
  python trade.py track 1304 8 1265 1330 1356 1382
                                    # track a position with EXPLICIT levels:
                                    # entry qty sl t1 t2 t3 (for delivery/swing)
"""
import os
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv
load_dotenv()

from src.trade_manager import set_trade, clear_trade, get_trade, current_pnl

CAPITAL     = float(os.getenv("CAPITAL", "100000"))
RISK_RUPEES = float(os.getenv("RISK_RUPEES", "4500"))
TICKER      = os.getenv("TICKER", "EMCURE")


def _live_price() -> float:
    try:
        import yfinance as yf
        info = yf.Ticker(f"{TICKER}.NS").fast_info
        return round(float(info.last_price), 2)
    except Exception:
        return 0.0


def cmd_buy(args: list[str]) -> None:
    if not args:
        print("Usage: python trade.py buy <entry_price> [qty]")
        sys.exit(1)

    entry = float(args[0])
    qty   = int(args[1]) if len(args) > 1 else int(CAPITAL / entry)

    if qty <= 0:
        print("❌ Invalid qty")
        sys.exit(1)

    state = set_trade(entry, qty, RISK_RUPEES)
    print(f"\n✅ Trade recorded — {TICKER}.NS")
    print(f"   Entry  ₹{state['entry']:,.2f}  ×  {state['qty']} sh")
    print(f"   SL     ₹{state['sl']:,.2f}  (−₹{round(entry - state['sl']):.0f}/sh)")
    print(f"   T1     ₹{state['t1']:,.2f}  (+₹10)")
    print(f"   T2     ₹{state['t2']:,.2f}  (+₹20)")
    print(f"   T3     ₹{state['t3']:,.2f}  (+₹25)")
    print(f"\n   WhatsApp alerts will fire when each level is crossed.")
    print(f"   Run 'python trade.py sell' when you exit.\n")


def cmd_sell(args: list[str]) -> None:
    trade = get_trade()
    if not trade:
        print("No active trade to close.")
        sys.exit(0)

    price = _live_price()
    pnl_data = current_pnl(price) if price > 0 else None
    clear_trade()

    print(f"\n✅ Trade closed — {TICKER}.NS")
    if pnl_data and price > 0:
        print(f"   Entry  ₹{pnl_data['entry']:,.2f}")
        print(f"   Exit   ₹{price:,.2f}  ({round(price - pnl_data['entry'], 2):+.2f}/sh)")
        print(f"   P&L    ₹{pnl_data['pnl']:+,.0f}")
    print()


def cmd_status(args: list[str]) -> None:
    trade = get_trade()
    if not trade:
        print("\nNo active trade.\n")
        sys.exit(0)

    price = _live_price()
    if price <= 0:
        print("❌ Could not fetch live price")
        sys.exit(1)

    pnl_data = current_pnl(price)
    hit = pnl_data["levels_hit"]

    print(f"\n📊 Active trade — {TICKER}.NS")
    print(f"   Entry    ₹{pnl_data['entry']:,.2f}  ×  {pnl_data['qty']} sh")
    print(f"   Current  ₹{price:,.2f}  ({pnl_data['pnl_per']:+.2f}/sh)")
    print(f"   P&L      ₹{pnl_data['pnl']:+,.0f}")
    print()
    for label, level in [("T3", pnl_data["t3"]), ("T2", pnl_data["t2"]),
                          ("T1", pnl_data["t1"]), ("SL", pnl_data["sl"])]:
        hit_str = " ✅" if label in hit else ""
        dist    = round(level - price, 2)
        print(f"   {label:<4} ₹{level:,.2f}  ({dist:+.2f}){hit_str}")
    print()


def cmd_holding(args: list[str]) -> None:
    """Read-only: query Zerodha for the live delivery holding (qty + average buy
    price) and print computed target/stop levels. Places no orders, writes no
    state — purely informational."""
    key    = os.getenv("KITE_API_KEY", "")
    secret = os.getenv("KITE_API_SECRET", "")
    if not key or not secret:
        print("❌ KITE_API_KEY / KITE_API_SECRET not set in .env")
        sys.exit(1)

    from src.broker import KiteBroker, _nse_symbol

    broker = KiteBroker(key, secret)
    if not broker.is_authenticated():
        print("❌ Kite not authenticated — today's token is missing/stale.")
        print("   The tracker auto-logs in at ~09:05 IST; run this after that,")
        print("   or send the TOKEN command first.")
        sys.exit(1)

    symbol = _nse_symbol(TICKER)
    qty, avg = 0, 0.0
    try:
        # Settled demat + unsettled T+1 (a just-opened delivery holding lives in
        # t1_quantity until it settles — same logic as KiteBroker.held_qty).
        for h in broker.kite.holdings():
            if h.get("tradingsymbol") == symbol:
                qty = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
                avg = float(h.get("average_price") or 0.0)
        # Same-day CNC before it settles into holdings.
        if qty == 0:
            for p in broker.kite.positions().get("net", []):
                if p.get("tradingsymbol") == symbol and p.get("product") == "CNC":
                    qty = int(p.get("quantity") or 0)
                    avg = float(p.get("average_price") or 0.0)
    except Exception as exc:
        print(f"❌ Could not query broker holdings: {exc}")
        sys.exit(1)

    if qty <= 0 or avg <= 0:
        print(f"\nNo {symbol} delivery holding found at the broker.\n")
        sys.exit(0)

    ltp = broker.get_ltp(TICKER)
    print(f"\n📦 Zerodha holding — {symbol}")
    print(f"   Qty      {qty} sh")
    print(f"   Avg buy  ₹{avg:,.2f}")
    if ltp > 0:
        print(f"   LTP      ₹{ltp:,.2f}  ({ltp - avg:+.2f}/sh)")
        print(f"   P&L      ₹{(ltp - avg) * qty:+,.0f}  ({(ltp / avg - 1) * 100:+.2f}%)")

    print(f"\n   Levels from avg ₹{avg:,.2f} (× {qty} sh):")
    print(f"   Rupee targets:  T1 ₹{avg+10:,.2f} (+₹{10*qty:,.0f})   "
          f"T2 ₹{avg+20:,.2f} (+₹{20*qty:,.0f})   T3 ₹{avg+25:,.2f} (+₹{25*qty:,.0f})")
    print(f"   %  targets:     T1 ₹{avg*1.02:,.2f} (+2%)   "
          f"T2 ₹{avg*1.04:,.2f} (+4%)   T3 ₹{avg*1.06:,.2f} (+6%)")
    print(f"   Stop options:   −2% ₹{avg*0.98:,.2f}   −3% ₹{avg*0.97:,.2f}   −5% ₹{avg*0.95:,.2f}")
    print(f"\n   To track + get level alerts, choose levels then run e.g.:")
    print(f"      python trade.py buy {avg:.2f} {qty}\n")


def cmd_track(args: list[str]) -> None:
    """Register a position with EXPLICIT target/stop levels for alert tracking.
    Use for delivery/swing holds where the fixed rupee levels don't fit."""
    if len(args) < 6:
        print("Usage: python trade.py track <entry> <qty> <sl> <t1> <t2> <t3>")
        sys.exit(1)

    entry, qty = float(args[0]), int(args[1])
    sl, t1, t2, t3 = float(args[2]), float(args[3]), float(args[4]), float(args[5])

    if qty <= 0:
        print("❌ Invalid qty")
        sys.exit(1)
    if not (sl < entry < t1 < t2 < t3):
        print(f"❌ Levels must satisfy SL < entry < T1 < T2 < T3")
        print(f"   got SL={sl} entry={entry} T1={t1} T2={t2} T3={t3}")
        sys.exit(1)

    state = set_trade(entry, qty, sl=sl, t1=t1, t2=t2, t3=t3)
    print(f"\n✅ Tracking {TICKER}.NS — {qty} sh @ ₹{state['entry']:,.2f}")
    print(f"   SL  ₹{state['sl']:,.2f}  ({state['sl'] - entry:+.2f}/sh,  risk ₹{round((state['sl']-entry)*qty):+,.0f})")
    print(f"   T1  ₹{state['t1']:,.2f}  (+₹{state['t1'] - entry:.2f}/sh,  +₹{round((state['t1']-entry)*qty):,.0f})")
    print(f"   T2  ₹{state['t2']:,.2f}  (+₹{state['t2'] - entry:.2f}/sh,  +₹{round((state['t2']-entry)*qty):,.0f})")
    print(f"   T3  ₹{state['t3']:,.2f}  (+₹{state['t3'] - entry:.2f}/sh,  +₹{round((state['t3']-entry)*qty):,.0f})")
    print(f"\n   Level alerts fire as each is crossed. 'python trade.py sell' to close.\n")


COMMANDS = {
    "buy": cmd_buy, "sell": cmd_sell, "status": cmd_status,
    "holding": cmd_holding, "track": cmd_track,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python trade.py buy <price> [qty]")
        print("       python trade.py sell")
        print("       python trade.py status")
        print("       python trade.py holding")
        print("       python trade.py track <entry> <qty> <sl> <t1> <t2> <t3>")
        sys.exit(1)

    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
