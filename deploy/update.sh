#!/usr/bin/env bash
# update.sh — pull latest main and restart every EmcurePriceTracker service.
#
# One command for both a human on the box and the GitHub Actions deploy job:
#   sudo bash /opt/emcure/deploy/update.sh
#
# What it does (idempotent, safe to re-run):
#   1. Hard-syncs the repo to origin/<branch> (default: main).
#   2. Refreshes Python deps from requirements-core.txt.
#   3. Re-installs a service's systemd unit from deploy/ when it drifted, so
#      ExecStart / env changes are picked up (this is why a bare `git pull`
#      isn't enough — e.g. the apps/ restructure changed every ExecStart).
#   4. daemon-reload, then restart every service on this host that runs from
#      $APP_DIR — discovered by WorkingDirectory, so it is agnostic to the
#      exact unit names (emcure-tracker/-bot/-radar, crypto, ...).
#   5. Prints each service's status; exits non-zero if any failed to come up.
#
# Runtime state and secrets are untouched: .env, trade_state.json,
# strategy_state.json and radar.db are all gitignored, so the hard reset
# never clobbers them.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/emcure}"
BRANCH="${DEPLOY_BRANCH:-main}"
APP_USER="${APP_USER:-emcure}"
VENV="$APP_DIR/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
[[ -d "$APP_DIR/.git" ]] || die "$APP_DIR is not a git checkout — run the first-time setup script instead."

# App module (bare name) → the unit file tracked in deploy/. Used to re-sync a
# drifted unit regardless of what the installed service happens to be named.
declare -A UNIT_SRC=(
  [main_headless]="$APP_DIR/deploy/emcure_price_tracker.service"
  [bot_server]="$APP_DIR/deploy/bot.service"
  [radar_headless]="$APP_DIR/deploy/radar.service"
  [crypto_headless]="$APP_DIR/deploy/crypto.service"
)

# Pull the app module out of a unit's ExecStart, handling both the current
# `-m apps.<name>` form and the pre-refactor `.../<name>.py` form.
extract_app() {
  local line
  line=$(grep -m1 '^ExecStart=' "$1" 2>/dev/null || true)
  if [[ "$line" =~ -m[[:space:]]+apps\.([a-z_]+) ]]; then echo "${BASH_REMATCH[1]}"; return; fi
  if [[ "$line" =~ /([a-z_]+)\.py([[:space:]]|$) ]]; then echo "${BASH_REMATCH[1]}"; return; fi
  echo ""
}

# ── 1. sync code ──────────────────────────────────────────────────────────────
info "Fetching origin/$BRANCH ..."
git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
OLD_REV=$(git -C "$APP_DIR" rev-parse HEAD)
git -C "$APP_DIR" reset --hard "origin/$BRANCH"
NEW_REV=$(git -C "$APP_DIR" rev-parse HEAD)
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
if [[ "$OLD_REV" == "$NEW_REV" ]]; then
  info "Already at $(git -C "$APP_DIR" rev-parse --short HEAD) — restarting anyway."
else
  info "Updated ${OLD_REV:0:7} → ${NEW_REV:0:7}"
fi

# ── 2. dependencies ───────────────────────────────────────────────────────────
info "Refreshing Python dependencies (core) ..."
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements-core.txt"

# ── 2b. dead-man's-switch watchdog (oneshot service + timer) ──────────────────
# Installed explicitly (not a long-running service) so the tracker's heartbeat
# is always watched. The timer self-gates to market hours, so it's cheap.
info "Installing watchdog timer ..."
install -m 644 "$APP_DIR/deploy/watchdog.service" /etc/systemd/system/emcure-watchdog.service
install -m 644 "$APP_DIR/deploy/watchdog.timer"   /etc/systemd/system/emcure-watchdog.timer

# ── 3. discover this project's services (name-agnostic, by WorkingDirectory) ──
mapfile -t UNITS < <(grep -lFx "WorkingDirectory=$APP_DIR" /etc/systemd/system/*.service 2>/dev/null || true)
[[ ${#UNITS[@]} -gt 0 ]] || die "No services found running from $APP_DIR — run the first-time setup script."

SERVICES=()
for unit in "${UNITS[@]}"; do
  svc=$(basename "$unit" .service)
  # The watchdog is a oneshot (fired by its timer) — never restart it as a
  # long-running service or health-check it for `active`, which it never is.
  [[ "$svc" == *watchdog* ]] && continue
  SERVICES+=("$svc")
  app=$(extract_app "$unit")
  src="${UNIT_SRC[$app]:-}"
  if [[ -n "$src" && -f "$src" ]]; then
    if ! cmp -s "$src" "$unit"; then
      info "Unit drifted — reinstalling $svc.service (module: apps.$app)"
      install -m 644 "$src" "$unit"
    fi
  else
    warn "$svc: could not map ExecStart to a tracked unit — leaving as-is."
  fi
done
info "Services: ${SERVICES[*]}"

# ── 4. reload + restart ───────────────────────────────────────────────────────
info "Reloading systemd and restarting services ..."
systemctl daemon-reload
systemctl enable --now emcure-watchdog.timer >/dev/null 2>&1 || warn "watchdog timer not enabled"
systemctl restart "${SERVICES[@]}"
sleep 3

# ── 5. report ─────────────────────────────────────────────────────────────────
FAILED=()
for svc in "${SERVICES[@]}"; do
  state=$(systemctl is-active "$svc" 2>/dev/null || echo "failed")
  if [[ "$state" == "active" ]]; then
    info "  ✓ $svc: $state"
  else
    warn "  ✗ $svc: $state"
    FAILED+=("$svc")
  fi
done

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo ""
  die "Failed to start: ${FAILED[*]} — check: journalctl -u ${FAILED[0]} -n 50"
fi

echo ""
info "Deploy complete — all services active at $(git -C "$APP_DIR" rev-parse --short HEAD)."
