#!/usr/bin/env python3
"""
Manual trade CLI.

Usage:
  python trade.py buy 1693          # record entry at ₹1693 (qty auto from CAPITAL)
  python trade.py buy 1693 60       # record entry at ₹1693, qty 60
  python trade.py sell              # close the trade
  python trade.py status            # show live P&L
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


COMMANDS = {"buy": cmd_buy, "sell": cmd_sell, "status": cmd_status}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python trade.py buy <price> [qty]")
        print("       python trade.py sell")
        print("       python trade.py status")
        sys.exit(1)

    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
