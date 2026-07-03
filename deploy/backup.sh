#!/usr/bin/env bash
# backup.sh — nightly snapshot of the runtime state + P&L ledgers.
#
# One VM re-image loses the entire live track record (emcure.db / radar.db)
# and any open-position state. This script writes a rotating local archive
# and, when configured, copies it off-box.
#
# Run by the emcure-backup.timer (17:00 IST, after close), or by hand:
#   sudo bash /opt/emcure/deploy/backup.sh
#
# Optional off-box copy — set ONE of these in /etc/default/emcure-backup:
#   BACKUP_OCI_BUCKET=<bucket>            # needs a configured `oci` CLI
#   BACKUP_RCLONE_REMOTE=<remote:path>    # needs a configured rclone
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/emcure}"
DEST="${BACKUP_DIR:-/var/backups/emcure}"
KEEP="${BACKUP_KEEP:-14}"
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Consistent SQLite copies (WAL-safe .backup) — plain cp as a fallback.
for db in emcure.db radar.db; do
  [[ -f "$APP_DIR/$db" ]] || continue
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$APP_DIR/$db" ".backup '$WORK/$db'"
  else
    cp "$APP_DIR/$db" "$WORK/$db"
  fi
done

for f in trade_state.json managed_state.json strategy_state.json alerts_sent.json; do
  [[ -f "$APP_DIR/$f" ]] && cp "$APP_DIR/$f" "$WORK/"
done

if [[ -z "$(ls -A "$WORK")" ]]; then
  echo "nothing to back up (no state files found in $APP_DIR)"
  exit 0
fi

TAR="$DEST/emcure-state-$STAMP.tar.gz"
tar -czf "$TAR" -C "$WORK" .
echo "backup written: $TAR ($(du -h "$TAR" | cut -f1))"

# Rotate: keep the newest $KEEP archives.
ls -1t "$DEST"/emcure-state-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

# Off-box copy (best effort — a failed upload must not fail the local backup).
if [[ -n "${BACKUP_OCI_BUCKET:-}" ]] && command -v oci >/dev/null 2>&1; then
  if oci os object put --bucket-name "$BACKUP_OCI_BUCKET" --file "$TAR" \
      --name "$(basename "$TAR")" --force; then
    echo "uploaded to OCI bucket $BACKUP_OCI_BUCKET"
  else
    echo "WARNING: OCI upload failed — local backup kept" >&2
  fi
elif [[ -n "${BACKUP_RCLONE_REMOTE:-}" ]] && command -v rclone >/dev/null 2>&1; then
  if rclone copy "$TAR" "$BACKUP_RCLONE_REMOTE"; then
    echo "uploaded via rclone to $BACKUP_RCLONE_REMOTE"
  else
    echo "WARNING: rclone upload failed — local backup kept" >&2
  fi
else
  echo "no off-box target configured (BACKUP_OCI_BUCKET / BACKUP_RCLONE_REMOTE) — local only"
fi
