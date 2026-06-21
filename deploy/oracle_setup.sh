#!/usr/bin/env bash
# oracle_setup.sh — Full deployment for Oracle Cloud Free Tier (Ubuntu 22.04 ARM)
#
# What it does:
#   1. Installs system packages (Python, nginx, certbot, git)
#   2. Opens firewall ports 80 and 443 (Oracle iptables + ufw)
#   3. Clones/updates the repo to /opt/emcure
#   4. Creates Python venv + installs requirements
#   5. Writes .env credentials
#   6. Installs two systemd services:
#        emcure-tracker  — headless alert engine (main_headless.py)
#        emcure-bot      — WhatsApp webhook server (bot_server.py)
#   7. Configures nginx reverse proxy + Let's Encrypt SSL (via DuckDNS domain)
#   8. Configures logrotate
#
# Usage (run as root on the Oracle VM):
#   curl -fsSL https://raw.githubusercontent.com/YOU/EmcurePriceTracker/main/deploy/oracle_setup.sh | sudo bash
#   — or —
#   sudo bash oracle_setup.sh

set -euo pipefail

APP_DIR="/opt/emcure"
TRACKER_SERVICE="emcure-tracker"
BOT_SERVICE="emcure-bot"
LOG_DIR="/var/log/emcure"
REPO_URL="${REPO_URL:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
step()  { echo -e "${CYAN}━━━ $* ━━━${NC}"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash oracle_setup.sh"

# ── Repo URL ──────────────────────────────────────────────────────────────────
if [[ -z "$REPO_URL" ]]; then
  read -rp "Git repo URL (e.g. https://github.com/you/EmcurePriceTracker): " REPO_URL
fi
[[ -n "$REPO_URL" ]] || die "REPO_URL is required"

# ── DuckDNS domain ────────────────────────────────────────────────────────────
echo ""
step "Free HTTPS domain (DuckDNS)"
echo "  1. Go to https://www.duckdns.org and sign in (Google/GitHub)"
echo "  2. Create a free subdomain, e.g.  emcure-bot"
echo "  3. Set its IP to this server's public IP (shown below)"
echo "  4. Copy the full domain: <YOUR_DOMAIN>"
echo ""
PUBLIC_IP=$(curl -s https://ifconfig.me 2>/dev/null || echo "unknown")
echo "  Your public IP: ${PUBLIC_IP}"
echo ""
read -rp "DuckDNS domain (e.g. <YOUR_DOMAIN>): " DOMAIN
[[ -n "$DOMAIN" ]] || die "Domain is required for HTTPS"

# ── 1. System packages ────────────────────────────────────────────────────────
step "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx ufw iptables-persistent fail2ban

# ── 2. Firewall ───────────────────────────────────────────────────────────────
step "Firewall — opening ports 80 and 443"
# Oracle Cloud has a REJECT ALL rule at iptables position 5.
# Use -I INPUT 5 (insert before REJECT) — never -A (appends after REJECT, has no effect).
iptables  -I INPUT 5 -m state --state NEW -p tcp --dport 80  -j ACCEPT || true
iptables  -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
ip6tables -I INPUT 5 -m state --state NEW -p tcp --dport 80  -j ACCEPT || true
ip6tables -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
netfilter-persistent save 2>/dev/null || true
ufw allow 80/tcp  2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true
info "Ports 80 and 443 opened."
warn "Also open them in Oracle Cloud Console → VCN → Security List → Ingress Rules"

# ── 3. Dedicated user ─────────────────────────────────────────────────────────
step "System user"
if ! id -u emcure &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin emcure
  info "Created user 'emcure'"
fi

# ── 4. Clone / update repo ────────────────────────────────────────────────────
step "Repository"
if [[ -d "$APP_DIR/.git" ]]; then
  info "Pulling latest changes..."
  git -C "$APP_DIR" pull --ff-only
else
  info "Cloning to $APP_DIR..."
  git clone "$REPO_URL" "$APP_DIR"
fi
chown -R emcure:emcure "$APP_DIR"

# ── 5. Python venv ────────────────────────────────────────────────────────────
step "Python environment"
sudo -u emcure python3 -m venv "$APP_DIR/venv"
sudo -u emcure "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u emcure "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-core.txt"
info "Dependencies installed."

# ── 6. .env credentials ───────────────────────────────────────────────────────
step "Credentials (.env)"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo ""
  warn "Enter your credentials (press Enter to skip optional fields):"
  echo ""

  read -rp "  NSE ticker [EMCURE]: "                                   TICKER;    TICKER="${TICKER:-EMCURE}"
  read -rp "  Refresh interval seconds [300]: "                        REFRESH;   REFRESH="${REFRESH:-300}"
  read -rp "  Trading capital in ₹ [500000]: "                         CAPITAL;   CAPITAL="${CAPITAL:-500000}"
  read -rp "  Twilio Account SID: "                                     TW_SID
  read -rp "  Twilio Auth Token: "                                      TW_TOKEN
  read -rp "  Twilio WhatsApp FROM (+14155238886 for sandbox): "        TW_FROM
  read -rp "  Your WhatsApp TO number (+91XXXXXXXXXX): "                TW_TO
  read -rp "  Telegram bot token (optional): "                          TG_TOKEN
  read -rp "  Telegram chat ID (optional): "                            TG_CHAT

  cat > "$ENV_FILE" <<EOF
TICKER=${TICKER}
REFRESH_SECONDS=${REFRESH}
CAPITAL=${CAPITAL}
FINBERT_MODEL_PATH=skip
HEADLESS=true

TWILIO_ACCOUNT_SID=${TW_SID}
TWILIO_AUTH_TOKEN=${TW_TOKEN}
TWILIO_WHATSAPP_FROM=${TW_FROM}
TWILIO_WHATSAPP_TO=${TW_TO}

TELEGRAM_TOKEN=${TG_TOKEN:-}
TELEGRAM_CHAT_ID=${TG_CHAT:-}
EOF
  chmod 600 "$ENV_FILE"
  chown emcure:emcure "$ENV_FILE"
  info ".env written."
else
  warn ".env already exists — skipping. Edit $ENV_FILE to change credentials."
fi

# ── 7. Log directory ──────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
chown emcure:emcure "$LOG_DIR"

# ── 8. Systemd — headless tracker ─────────────────────────────────────────────
step "Systemd service: $TRACKER_SERVICE"
cat > "/etc/systemd/system/${TRACKER_SERVICE}.service" <<EOF
[Unit]
Description=Emcure Headless Alert Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=emcure
Group=emcure
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python3 ${APP_DIR}/main_headless.py
Restart=on-failure
RestartSec=30
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=LOKY_MAX_CPU_COUNT=1
Environment=OMP_NUM_THREADS=1
StandardOutput=append:${LOG_DIR}/tracker.log
StandardError=append:${LOG_DIR}/tracker.err

[Install]
WantedBy=multi-user.target
EOF

# ── 9. Systemd — WhatsApp bot ─────────────────────────────────────────────────
step "Systemd service: $BOT_SERVICE"
cat > "/etc/systemd/system/${BOT_SERVICE}.service" <<EOF
[Unit]
Description=Emcure WhatsApp Trade Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=emcure
Group=emcure
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python3 ${APP_DIR}/bot_server.py
Restart=always
RestartSec=10
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=LOKY_MAX_CPU_COUNT=1
Environment=OMP_NUM_THREADS=1
Environment=BOT_PORT=5001
StandardOutput=append:${LOG_DIR}/bot.log
StandardError=append:${LOG_DIR}/bot.err

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$TRACKER_SERVICE" "$BOT_SERVICE"
info "Services enabled."

# ── 10. Nginx rate-limit zone ─────────────────────────────────────────────────
step "Nginx rate-limit zone"
cat > /etc/nginx/conf.d/emcure-ratelimit.conf <<'EOF'
# 10 requests/minute per IP on the /whatsapp webhook
limit_req_zone $binary_remote_addr zone=webhook:10m rate=10r/m;
EOF

# ── 11. Nginx reverse proxy ───────────────────────────────────────────────────
step "Nginx reverse proxy"
NGINX_CONF="/etc/nginx/sites-available/emcure-bot"
cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # Security headers
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header X-XSS-Protection "1; mode=block" always;

    # Rate-limited WhatsApp webhook
    location /whatsapp {
        limit_req zone=webhook burst=5 nodelay;
        proxy_pass         http://127.0.0.1:5001;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }

    location / {
        proxy_pass         http://127.0.0.1:5001;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }
}
EOF
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/emcure-bot
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
info "Nginx configured for $DOMAIN"

# ── 12. SSL via Let's Encrypt ─────────────────────────────────────────────────
step "SSL certificate (Let's Encrypt)"
info "Requesting certificate for $DOMAIN ..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@${DOMAIN}" --redirect
info "SSL certificate installed. Auto-renews via certbot timer."

# ── 12a. HSTS header (only safe after SSL is live) ───────────────────────────
# certbot --redirect converts port 80 to HTTPS; now safe to add HSTS
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
if "Strict-Transport-Security" not in content:
    content = content.replace(
        "    add_header X-XSS-Protection",
        '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;\n    add_header X-XSS-Protection',
    )
    with open(path, "w") as f:
        f.write(content)
    print("HSTS header added.")
PYEOF
nginx -t && systemctl reload nginx

# ── 13. SSH hardening ─────────────────────────────────────────────────────────
step "SSH hardening"
SSHD="/etc/ssh/sshd_config"
sed -i 's/^#*\s*PasswordAuthentication.*/PasswordAuthentication no/'  "$SSHD"
sed -i 's/^#*\s*PermitRootLogin.*/PermitRootLogin no/'                 "$SSHD"
grep -q "^MaxAuthTries" "$SSHD" \
  && sed -i 's/^MaxAuthTries.*/MaxAuthTries 3/' "$SSHD" \
  || echo "MaxAuthTries 3" >> "$SSHD"
systemctl restart ssh
info "SSH hardened — password auth off, root login off, max 3 tries."

# ── 14. fail2ban ──────────────────────────────────────────────────────────────
step "fail2ban"
systemctl enable fail2ban
systemctl start  fail2ban
info "fail2ban active."

# ── 15. Logrotate ─────────────────────────────────────────────────────────────
cat > /etc/logrotate.d/emcure <<EOF
${LOG_DIR}/*.log ${LOG_DIR}/*.err {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 emcure emcure
}
EOF

# ── 16. Start services ────────────────────────────────────────────────────────
step "Starting services"
systemctl restart "$BOT_SERVICE" "$TRACKER_SERVICE"
sleep 3

BOT_STATUS=$(systemctl is-active "$BOT_SERVICE" 2>/dev/null || echo "failed")
TRK_STATUS=$(systemctl is-active "$TRACKER_SERVICE" 2>/dev/null || echo "failed")

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Deployment complete!${NC}"
echo ""
echo "  WhatsApp bot  : $BOT_STATUS  — https://${DOMAIN}/whatsapp"
echo "  Alert engine  : $TRK_STATUS"
echo ""
echo "  Set this as your Twilio webhook:"
echo -e "  ${CYAN}https://${DOMAIN}/whatsapp${NC}"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status $BOT_SERVICE"
echo "    sudo systemctl status $TRACKER_SERVICE"
echo "    tail -f ${LOG_DIR}/bot.log"
echo "    tail -f ${LOG_DIR}/tracker.log"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
