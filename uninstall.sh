#!/usr/bin/env bash
#
# NCAP Checker - removal script
# ---------------------------------------------------------------------------
#   sudo ./uninstall.sh              stop and remove services, nginx and sudoers
#   sudo ./uninstall.sh --purge      also delete /opt/ncap-checker and the user
#
# Without --purge your database, screenshots and configuration are kept so you
# can re-install later and pick up where you left off.
#
set -euo pipefail

INSTALL_DIR="/opt/ncap-checker"
SERVICE_USER="ncap"
PURGE="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)   INSTALL_DIR="$2"; shift 2 ;;
    --user)  SERVICE_USER="$2"; shift 2 ;;
    --purge) PURGE="yes"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root: sudo ./uninstall.sh"; exit 1; }

say() { printf '\n==> %s\n' "$1"; }

say "Stopping services"
systemctl stop ncap-web ncap-scheduler 2>/dev/null || true
systemctl disable ncap-web ncap-scheduler 2>/dev/null || true
rm -f /etc/systemd/system/ncap-web.service /etc/systemd/system/ncap-scheduler.service
systemctl daemon-reload
echo "    services stopped and removed"

say "Removing nginx site"
rm -f /etc/nginx/sites-enabled/ncap-checker /etc/nginx/sites-available/ncap-checker
if command -v nginx >/dev/null 2>&1; then
  nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
fi
echo "    nginx site removed"

say "Removing sudoers rule"
rm -f /etc/sudoers.d/ncap-checker
echo "    /etc/sudoers.d/ncap-checker removed"

if [[ "$PURGE" == "yes" ]]; then
  say "Purging $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  rm -rf "/var/log/ncap-checker"
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  echo "    application, logs and the '$SERVICE_USER' account were deleted"
else
  say "Keeping your data"
  echo "    $INSTALL_DIR/instance  (database, screenshots, browser profiles)"
  echo "    $INSTALL_DIR/.env      (configuration)"
  echo "    Re-run install.sh at any time to restore the service."
fi

printf '\nNCAP Checker removed.\n\n'
