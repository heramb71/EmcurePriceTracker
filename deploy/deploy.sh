#!/usr/bin/env bash
# deploy.sh — one-shot server setup for EmcurePriceTracker (headless mode)
#
# Run this on a fresh Ubuntu 22.04 LTS server (DigitalOcean $6/mo Droplet recommended):
#   bash deploy.sh
#
# What it does:
#   1. Creates a dedicated 'emcure' system user
#   2. Clones the repo to /opt/emcure
#   3. Creates a Python venv + installs requirements-core.txt
#   4. Prompts for .env credentials and writes /opt/emcure/.env
#   5. Installs + enables the systemd service
#   6. Configures logrotate (30-day retention)
#   7. Starts the service and tails the log

set -euo pipefail

APP_DIR="/opt/emcure"
SERVICE_NAME="emcure-tracker"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="/var/log/emcure/tracker.log"
ERR_FILE="/var/log/emcure/tracker.err"
REPO_URL="${REPO_URL:-}"  # set via env or prompted below
PYTHON="python3"

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash deploy.sh"

# ── repo URL ──────────────────────────────────────────────────────────────────
if [[ -z "$REPO_URL" ]]; then
  read -rp "Git repo URL (e.g. https://github.com/you/EmcurePriceTracker): " REPO_URL
fi
[[ -n "$REPO_URL" ]] || die "REPO_URL is required"

# ── 1. system packages ────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git logrotate

# ── 2. dedicated user ─────────────────────────────────────────────────────────
if ! id -u emcure &>/dev/null; then
  info "Creating system user 'emcure'..."
  useradd --system --no-create-home --shell /usr/sbin/nologin emcure
fi

# ── 3. clone / update repo ────────────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
  info "Repo exists — pulling latest..."
  git -C "$APP_DIR" pull --ff-only
else
  info "Cloning repo to $APP_DIR..."
  git clone "$REPO_URL" "$APP_DIR"
fi
chown -R emcure:emcure "$APP_DIR"

# ── 4. python venv + dependencies ────────────────────────────────────────────
info "Setting up Python venv..."
sudo -u emcure $PYTHON -m venv "$APP_DIR/venv"
sudo -u emcure "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u emcure "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-core.txt"
info "Dependencies installed (core only — FinBERT/torch skipped to save RAM)."

# ── 5. .env credentials ───────────────────────────────────────────────────────
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  info "Setting up .env credentials..."
  echo ""
  warn "Enter your credentials (press Enter to leave optional fields blank):"
  echo ""

  read -rp "  NSE ticker [EMCURE]: "              TICKER
  TICKER="${TICKER:-EMCURE}"

  read -rp "  Refresh interval in seconds [300]: " REFRESH
  REFRESH="${REFRESH:-300}"

  read -rp "  Telegram bot token (optional): "    TG_TOKEN
  read -rp "  Telegram chat ID (optional): "      TG_CHAT

  read -rp "  Twilio Account SID (optional): "    TW_SID
  read -rp "  Twilio Auth Token (optional): "     TW_TOKEN
  read -rp "  Twilio WhatsApp FROM number (e.g. +14155238886): " TW_FROM
  read -rp "  Your WhatsApp TO number (e.g. +919876543210): "    TW_TO

  cat > "$ENV_FILE" <<EOF
TICKER=${TICKER}
REFRESH_SECONDS=${REFRESH}
FINBERT_MODEL_PATH=skip

TELEGRAM_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}

TWILIO_ACCOUNT_SID=${TW_SID}
TWILIO_AUTH_TOKEN=${TW_TOKEN}
TWILIO_WHATSAPP_FROM=${TW_FROM}
TWILIO_WHATSAPP_TO=${TW_TO}
EOF
  chmod 600 "$ENV_FILE"
  chown emcure:emcure "$ENV_FILE"
  info ".env written to $ENV_FILE"
else
  warn ".env already exists — skipping credential prompt. Edit $ENV_FILE to change."
fi

# ── 6. log directory ──────────────────────────────────────────────────────────
mkdir -p /var/log/emcure
chown emcure:emcure /var/log/emcure

# ── 7. systemd service ────────────────────────────────────────────────────────
info "Installing systemd service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=EmcurePriceTracker — Headless NSE Swing Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=emcure
Group=emcure
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python3 -m apps.main_headless
Restart=on-failure
RestartSec=30
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=LOKY_MAX_CPU_COUNT=1
Environment=OMP_NUM_THREADS=1
StandardOutput=append:${LOG_FILE}
StandardError=append:${ERR_FILE}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
info "Service enabled: $SERVICE_NAME"

# ── 8. logrotate ─────────────────────────────────────────────────────────────
cat > "/etc/logrotate.d/emcure-tracker" <<EOF
/var/log/emcure/*.log /var/log/emcure/*.err {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 emcure emcure
    postrotate
        systemctl kill --signal=USR1 ${SERVICE_NAME} 2>/dev/null || true
    endscript
}
EOF
info "Log rotation configured (30-day retention)."

# ── 9. start ──────────────────────────────────────────────────────────────────
info "Starting service..."
systemctl restart "$SERVICE_NAME"
sleep 3

STATUS=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
if [[ "$STATUS" == "active" ]]; then
  echo ""
  echo -e "${GREEN}✓ EmcurePriceTracker is live!${NC}"
  echo ""
  echo "  Useful commands:"
  echo "    sudo systemctl status $SERVICE_NAME"
  echo "    sudo journalctl -u $SERVICE_NAME -f"
  echo "    tail -f $LOG_FILE"
  echo "    sudo systemctl stop $SERVICE_NAME"
  echo ""
  info "Tailing log (Ctrl+C to exit — service keeps running):"
  tail -f "$LOG_FILE"
else
  die "Service failed to start. Check: sudo journalctl -u $SERVICE_NAME -n 50"
fi
