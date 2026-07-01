#!/bin/bash
# Start the WhatsApp Trade Bot + ngrok tunnel in one command.
# Usage: ./start_bot.sh

set -e
cd "$(dirname "$0")"

BOT_PORT=${BOT_PORT:-5001}

# ── Activate venv ────────────────────────────────────────────────────────────
if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
else
  echo "❌ No virtual environment found (.venv or venv)"
  exit 1
fi

# ── Check ngrok ──────────────────────────────────────────────────────────────
if ! command -v ngrok &>/dev/null; then
  echo ""
  echo "❌ ngrok not found. Install it first:"
  echo "   brew install ngrok"
  echo "   Then sign up free at https://ngrok.com and run:"
  echo "   ngrok config add-authtoken <your-token>"
  echo ""
  exit 1
fi

# ── Start bot server ─────────────────────────────────────────────────────────
echo ""
echo "🤖 Starting WhatsApp Trade Bot on port $BOT_PORT..."
python -m apps.bot_server &
BOT_PID=$!

sleep 2

# Verify it started
if ! kill -0 $BOT_PID 2>/dev/null; then
  echo "❌ Bot server failed to start. Check for errors above."
  exit 1
fi

# ── Start ngrok tunnel ───────────────────────────────────────────────────────
echo "🌐 Starting ngrok tunnel..."
ngrok http $BOT_PORT --log=stdout --log-format=json > /tmp/ngrok_bot.log 2>&1 &
NGROK_PID=$!

sleep 3

# ── Get tunnel URL from ngrok API ────────────────────────────────────────────
TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tunnels = d.get('tunnels', [])
    https = [t for t in tunnels if t['public_url'].startswith('https')]
    print((https or tunnels)[0]['public_url'])
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$TUNNEL_URL" ]; then
  echo "❌ Could not get ngrok tunnel URL."
  echo "   Check: http://localhost:4040"
  kill $BOT_PID $NGROK_PID 2>/dev/null
  exit 1
fi

WEBHOOK_URL="$TUNNEL_URL/whatsapp"

# ── Print setup instructions ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Bot running!  PID=$BOT_PID"
echo "✅ Tunnel:       $TUNNEL_URL"
echo ""
echo "📱 One-time Twilio setup (only needed once):"
echo ""
echo "   1. Open: https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn"
echo "   2. Scroll to 'Sandbox Configuration'"
echo "   3. Set 'When a message comes in' to:"
echo ""
echo "      $WEBHOOK_URL"
echo ""
echo "   4. Method: HTTP POST  →  Save"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📲 Then WhatsApp +14155238886 with:"
echo "   BUY 1693       — record entry"
echo "   STATUS         — live P&L"
echo "   SELL           — close trade"
echo "   HELP           — all commands"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Press Ctrl+C to stop."
echo ""

# ── Cleanup on exit ──────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Stopping bot and tunnel..."
  kill $BOT_PID $NGROK_PID 2>/dev/null
  exit 0
}
trap cleanup INT TERM

wait $BOT_PID
