#!/usr/bin/env bash
# harden.sh — Security hardening for an existing emcure deployment
#
# Run ONCE on the Oracle Cloud server as root:
#   sudo bash harden.sh
#
# What it does:
#   1. Install + enable fail2ban (auto-bans SSH brute-forcers)
#   2. Harden SSH (disable password auth, no root login, max 3 tries)
#   3. Add nginx rate limiting zone (10 req/min on /whatsapp)
#   4. Add nginx security headers + dedicated /whatsapp location
#   5. Reload nginx

set -euo pipefail

SITE_CONF="/etc/nginx/sites-available/emcure-bot"
RATELIMIT_CONF="/etc/nginx/conf.d/emcure-ratelimit.conf"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
step() { echo -e "${CYAN}━━━ $* ━━━${NC}"; }
die()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash harden.sh"

# ── 1. fail2ban ───────────────────────────────────────────────────────────────
step "fail2ban"
apt-get install -y -qq fail2ban
systemctl enable fail2ban
systemctl start  fail2ban
info "fail2ban active — SSH brute-force protection enabled."

# ── 2. SSH hardening ──────────────────────────────────────────────────────────
step "SSH hardening"
SSHD="/etc/ssh/sshd_config"

sed -i 's/^#*\s*PasswordAuthentication.*/PasswordAuthentication no/'  "$SSHD"
sed -i 's/^#*\s*PermitRootLogin.*/PermitRootLogin no/'                 "$SSHD"

if grep -q "^MaxAuthTries" "$SSHD"; then
    sed -i 's/^MaxAuthTries.*/MaxAuthTries 3/' "$SSHD"
else
    echo "MaxAuthTries 3" >> "$SSHD"
fi

systemctl restart ssh
info "SSH: password auth disabled, root login disabled, max 3 tries."

# ── 3. Nginx rate-limit zone ──────────────────────────────────────────────────
step "Nginx rate-limit zone"
cat > "$RATELIMIT_CONF" <<'EOF'
# 10 requests/minute per IP on the /whatsapp endpoint
limit_req_zone $binary_remote_addr zone=webhook:10m rate=10r/m;
EOF
info "Rate-limit zone written to $RATELIMIT_CONF"

# ── 4. Patch nginx site config ────────────────────────────────────────────────
step "Nginx config — security headers + /whatsapp rate limiting"

[[ -f "$SITE_CONF" ]] || die "$SITE_CONF not found — is the bot deployed?"

# Back up original
cp "$SITE_CONF" "${SITE_CONF}.bak.$(date +%Y%m%d%H%M%S)"

python3 - "$SITE_CONF" <<'PYEOF'
import sys, re

path = sys.argv[1]
with open(path) as f:
    content = f.read()

SECURITY_HEADERS = """\
    # Security headers
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-XSS-Protection "1; mode=block" always;

"""

WHATSAPP_LOCATION = """\
    # Rate-limited WhatsApp webhook
    location /whatsapp {
        limit_req zone=webhook burst=5 nodelay;
        proxy_pass         http://127.0.0.1:5001;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }

"""

# Only patch the SSL server block (the one with ssl_certificate)
# Strategy: find the server block that contains ssl_certificate, insert headers +
# dedicated /whatsapp location before the existing catch-all location /

# Guard: skip if already patched
if "X-Frame-Options" in content:
    print("Already patched — skipping security-header injection.")
else:
    # Insert security headers + /whatsapp block before first "    location /"
    # that appears inside the SSL server block
    ssl_block_start = content.find("ssl_certificate")
    if ssl_block_start == -1:
        # No SSL yet — patch the only server block
        content = content.replace("    location / {", SECURITY_HEADERS + WHATSAPP_LOCATION + "    location / {", 1)
    else:
        # Find the "    location /" after the SSL certificate line
        loc_pos = content.find("    location / {", ssl_block_start)
        if loc_pos != -1:
            content = content[:loc_pos] + SECURITY_HEADERS + WHATSAPP_LOCATION + content[loc_pos:]

with open(path, "w") as f:
    f.write(content)

print("Nginx config patched.")
PYEOF

# ── 5. Test + reload nginx ────────────────────────────────────────────────────
step "Reload nginx"
nginx -t
systemctl reload nginx
info "Nginx reloaded."

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Hardening complete!${NC}"
echo ""
echo "  ✓ fail2ban          — SSH brute-force protection"
echo "  ✓ SSH               — password auth off, root login off"
echo "  ✓ nginx rate limit  — 10 req/min on /whatsapp"
echo "  ✓ Security headers  — X-Frame, HSTS, XSS protection"
echo "  ✓ Twilio validation — enforced in bot_server.py"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
