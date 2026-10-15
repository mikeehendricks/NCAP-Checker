#!/usr/bin/env bash
#
# NCAP Checker - Ubuntu server installer
# ---------------------------------------------------------------------------
# Installs the app to /opt/ncap-checker as a dedicated `ncap` user, behind
# nginx, with two systemd units (web + background scheduler).
#
#   sudo ./install.sh                       # install / update in place
#   sudo ./install.sh --local               # install from this directory
#   sudo ./install.sh --no-browser          # skip the Playwright download
#   sudo ./install.sh --help                # all options
#
# Re-running the installer is safe: it refreshes code, dependencies and units
# without touching your database, screenshots or saved vehicles.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override with flags)
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/ncap-checker"
REPO_URL="https://github.com/mikeehendricks/NCAP-Checker.git"
BRANCH="main"
SERVICE_USER="ncap"
PORT="8000"
INSTALL_BROWSER="yes"
INSTALL_NGINX="yes"
LOCAL_SOURCE="no"
RUN_MIGRATIONS="yes"
DOMAIN=""

APP_NAME="NCAP Checker"

# Admin bootstrap (non-interactive mode)
ADMIN_USER="${NCAP_ADMIN_USER:-}"
ADMIN_EMAIL="${NCAP_ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${NCAP_ADMIN_PASSWORD:-}"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi

step()  { printf '\n%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"; }
info()  { printf '    %s\n' "$1"; }
ok()    { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn()  { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
die()   { printf '\n%sERROR:%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; exit 1; }

usage() {
  cat <<'USAGE'
NCAP Checker installer

Usage: sudo ./install.sh [options]

Options:
  --dir PATH          Install directory        (default: /opt/ncap-checker)
  --repo URL          Git repository to clone  (default: the GitHub project)
  --branch NAME       Branch to track          (default: main)
  --user NAME         Service account          (default: ncap)
  --port PORT         Gunicorn bind port       (default: 8000)
  --domain NAME       nginx server_name        (default: _)
  --local             Install from the current directory instead of git
  --no-browser        Skip Playwright/Chromium (HTTP engine only)
  --no-nginx          Do not install or configure nginx
  --help              Show this message

Environment (optional, for unattended installs):
  NCAP_ADMIN_USER, NCAP_ADMIN_EMAIL, NCAP_ADMIN_PASSWORD
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
    --repo)       REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
    --branch)     BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --user)       SERVICE_USER="${2:?--user needs a value}"; shift 2 ;;
    --port)       PORT="${2:?--port needs a value}"; shift 2 ;;
    --domain)     DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --local)      LOCAL_SOURCE="yes"; shift ;;
    --no-browser) INSTALL_BROWSER="no"; shift ;;
    --no-nginx)   INSTALL_NGINX="no"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "Unknown option: $1 (try --help)" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
step "1/10 Checking the environment"
# ---------------------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || die "Run this installer as root (sudo ./install.sh)."

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "Detected: ${PRETTY_NAME:-unknown Linux}"
  case "${ID:-}" in
    ubuntu|debian|linuxmint|pop|raspbian) : ;;
    *) warn "${ID:-this distribution} is not officially supported; continuing anyway." ;;
  esac
else
  warn "/etc/os-release is missing; assuming a Debian-like system."
fi

command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)."
PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || die "python3 is required but was not found."
ok "python3 found at $PYTHON_BIN"

# ---------------------------------------------------------------------------
step "2/10 Installing system packages"
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
APT_PACKAGES=(python3 python3-venv python3-dev python3-pip build-essential git curl
              ca-certificates libssl-dev libffi-dev pkg-config)

if [[ "$INSTALL_NGINX" == "yes" ]]; then
  APT_PACKAGES+=(nginx)
fi

if [[ "$INSTALL_BROWSER" == "yes" ]]; then
  # Chromium runtime dependencies (Playwright's `install-deps` pulls the same set).
  APT_PACKAGES+=(libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2
                 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
                 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0
                 fonts-liberation fonts-noto-color-emoji)
fi

apt-get update -qq
# shellcheck disable=SC2068
apt-get install -y -qq --no-install-recommends ${APT_PACKAGES[@]} >/dev/null
ok "Installed: ${#APT_PACKAGES[@]} packages"

# ---------------------------------------------------------------------------
step "3/10 Creating the service account"
# ---------------------------------------------------------------------------
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  ok "User '$SERVICE_USER' already exists"
else
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin \
          --comment "$APP_NAME service" "$SERVICE_USER"
  ok "Created system user '$SERVICE_USER'"
fi

# ---------------------------------------------------------------------------
step "4/10 Getting the application code"
# ---------------------------------------------------------------------------
FRESH_INSTALL="no"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Existing checkout found - updating it"
  git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" 2>/dev/null || true
  git -C "$INSTALL_DIR" fetch --tags origin "$BRANCH" || warn "git fetch failed; keeping current code"
  git -C "$INSTALL_DIR" checkout "$BRANCH" 2>/dev/null || true
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" 2>/dev/null \
    || warn "Could not fast-forward (local changes?). Leaving code as-is."
  ok "Updated $INSTALL_DIR"
elif [[ "$LOCAL_SOURCE" == "yes" ]]; then
  FRESH_INSTALL="yes"
  info "Copying from $SCRIPT_DIR"
  mkdir -p "$INSTALL_DIR"
  # Preserve runtime data when re-installing over an existing directory.
  for item in app install.sh uninstall.sh scripts requirements.txt requirements-dev.txt \
              wsgi.py run.py scheduler_worker.py pytest.ini LICENSE README.md .env.example; do
    [[ -e "$SCRIPT_DIR/$item" ]] && cp -a "$SCRIPT_DIR/$item" "$INSTALL_DIR/"
  done
  mkdir -p "$INSTALL_DIR/tests"
  [[ -d "$SCRIPT_DIR/tests" ]] && cp -a "$SCRIPT_DIR/tests/." "$INSTALL_DIR/tests/" || true
  ok "Copied application files"
else
  FRESH_INSTALL="yes"
  info "Cloning $REPO_URL ($BRANCH)"
  if ! git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"; then
    die "git clone failed. Check the repository URL and your network access."
  fi
  ok "Cloned into $INSTALL_DIR"
fi

cd "$INSTALL_DIR"
[[ -f wsgi.py ]] || die "wsgi.py is missing in $INSTALL_DIR - the checkout looks wrong."

# ---------------------------------------------------------------------------
step "5/10 Creating the virtual environment"
# ---------------------------------------------------------------------------
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  info "Creating a fresh virtual environment"
  rm -rf "$INSTALL_DIR/.venv"
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade --quiet pip setuptools wheel
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python dependencies installed"

# ---------------------------------------------------------------------------
step "6/10 Preparing data directories and configuration"
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR/instance/data/screenshots" "$INSTALL_DIR/instance/data/browser-profiles"
LOG_DIR="/var/log/ncap-checker"
mkdir -p "$LOG_DIR"

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')"
  cat > "$ENV_FILE" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
NCAP_ENV=production
NCAP_SECRET_KEY=$SECRET
NCAP_INSTANCE_DIR=$INSTALL_DIR/instance
NCAP_DATA_DIR=$INSTALL_DIR/instance/data
NCAP_LOG_FILE=$LOG_DIR/ncap.log
NCAP_TIMEZONE=Asia/Manila
NCAP_LOG_LEVEL=INFO

# One process serves the site, another runs the scheduler. The units set
# NCAP_ROLE=web and NCAP_ROLE=scheduler respectively.
# NCAP_ROLE=all

# Browser engine (Leave enabled: the NCAP portals need JavaScript.)
NCAP_PLAYWRIGHT_HEADLESS=true

# Self-update from the admin panel
NCAP_UPDATE_MODE=git
NCAP_GIT_REMOTE=origin
NCAP_GIT_BRANCH=$BRANCH
NCAP_SERVICE_WEB=ncap-web
NCAP_SERVICE_SCHEDULER=ncap-scheduler

# Set to true once you terminate TLS at nginx and use https://
NCAP_SESSION_SECURE=false
EOF
  chmod 600 "$ENV_FILE"
  ok "Wrote $ENV_FILE (mode 600)"
else
  ok "Keeping the existing .env (not overwritten)"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
chmod 750 "$INSTALL_DIR/instance"

# ---------------------------------------------------------------------------
step "7/10 Initialising the database"
# ---------------------------------------------------------------------------
run_manage() {
  # Run a Flask CLI command as the service user with the production env loaded.
  set -a; # shellcheck disable=SC1090
  . "$ENV_FILE"; set +a
  sudo -u "$SERVICE_USER" env NCAP_ENV=production \
    "$INSTALL_DIR/.venv/bin/python" -m flask --app wsgi "$@"
}

run_manage init-db
ok "Schema, default settings and the five NCAP portals are ready"

# ---------------------------------------------------------------------------
step "8/10 Installing the browser engine"
# ---------------------------------------------------------------------------
if [[ "$INSTALL_BROWSER" == "yes" ]]; then
  if "$INSTALL_DIR/.venv/bin/python" -c "import playwright" >/dev/null 2>&1; then
    info "Downloading Chromium for Playwright (this takes a minute)"
    if sudo -u "$SERVICE_USER" env HOME="$INSTALL_DIR" \
        "$INSTALL_DIR/.venv/bin/python" -m playwright install chromium; then
      ok "Chromium installed"
    else
      warn "Chromium download failed. The app still runs, but portals that need"
      warn "JavaScript will report 'blocked'. You can retry later with:"
      warn "  sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python -m playwright install chromium"
    fi
  else
    warn "playwright is not in requirements.txt output; skipping the browser"
  fi
else
  info "Skipped (--no-browser): only the HTTP engine will be available"
fi

# ---------------------------------------------------------------------------
step "9/10 Installing systemd units and nginx"
# ---------------------------------------------------------------------------
sed -e "s#/opt/ncap-checker#$INSTALL_DIR#g" \
    -e "s#^User=ncap\$#User=$SERVICE_USER#" \
    -e "s#^Group=ncap\$#Group=$SERVICE_USER#" \
    -e "s#--bind 127.0.0.1:8000#--bind 127.0.0.1:$PORT#" \
    "$INSTALL_DIR/scripts/ncap-web.service" > /etc/systemd/system/ncap-web.service

sed -e "s#/opt/ncap-checker#$INSTALL_DIR#g" \
    -e "s#^User=ncap\$#User=$SERVICE_USER#" \
    -e "s#^Group=ncap\$#Group=$SERVICE_USER#" \
    "$INSTALL_DIR/scripts/ncap-scheduler.service" > /etc/systemd/system/ncap-scheduler.service

# Passwordless restart so the admin panel's Update button can restart the units.
if [[ -d /etc/sudoers.d ]]; then
  sed -e "s#^ncap #$SERVICE_USER #" \
      "$INSTALL_DIR/scripts/sudoers-ncap.conf" > /etc/sudoers.d/ncap-checker
  chmod 440 /etc/sudoers.d/ncap-checker
  visudo -cf /etc/sudoers.d/ncap-checker >/dev/null || rm -f /etc/sudoers.d/ncap-checker
  ok "Installed /etc/sudoers.d/ncap-checker"
fi

if [[ "$INSTALL_NGINX" == "yes" ]]; then
  sed -e "s#/opt/ncap-checker#$INSTALL_DIR#g" \
      -e "s#server 127.0.0.1:8000;#server 127.0.0.1:$PORT;#" \
      -e "s#^    server_name _;#    server_name ${DOMAIN:-_};#" \
      "$INSTALL_DIR/scripts/nginx-ncap.conf" > /etc/nginx/sites-available/ncap-checker
  ln -sf /etc/nginx/sites-available/ncap-checker /etc/nginx/sites-enabled/ncap-checker
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && ok "nginx configuration is valid"
fi

systemctl daemon-reload
systemctl enable --quiet ncap-web ncap-scheduler
systemctl restart ncap-web
systemctl restart ncap-scheduler
sleep 2
ok "Units installed and started"

# ---------------------------------------------------------------------------
step "10/10 Creating the administrator"
# ---------------------------------------------------------------------------
ADMIN_COUNT="$(sudo -u "$SERVICE_USER" env NCAP_ENV=production \
  "$INSTALL_DIR/.venv/bin/python" - <<'PY'
from app import create_app
from app.models import User
app = create_app()
with app.app_context():
    print(User.query.filter_by(is_admin=True).count())
PY
)"

if [[ "${ADMIN_COUNT:-0}" != "0" ]]; then
  ok "An administrator already exists - skipping"
else
  if [[ -z "$ADMIN_USER" ]]; then
    printf '\n%sCreate the administrator account%s\n' "$C_BOLD" "$C_RESET"
    read -r -p "    Username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"
    read -r -p "    E-mail: " ADMIN_EMAIL
    while true; do
      read -r -s -p "    Password (min. 10 characters): " ADMIN_PASSWORD; printf '\n'
      read -r -s -p "    Confirm password: " ADMIN_PASSWORD_CONFIRM; printf '\n'
      [[ "$ADMIN_PASSWORD" == "$ADMIN_PASSWORD_CONFIRM" ]] || { warn "Passwords did not match."; continue; }
      [[ ${#ADMIN_PASSWORD} -ge 10 ]] || { warn "Use at least 10 characters."; continue; }
      break
    done
  fi

  if [[ -n "$ADMIN_USER" && -n "$ADMIN_EMAIL" && -n "$ADMIN_PASSWORD" ]]; then
    sudo -u "$SERVICE_USER" env NCAP_ENV=production \
      "$INSTALL_DIR/.venv/bin/python" -m flask --app wsgi create-admin \
      "$ADMIN_USER" "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD" >/dev/null
    ok "Administrator '$ADMIN_USER' created"
  else
    warn "No administrator created. Finish the setup in your browser at /setup."
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n%s%s================================================%s\n' "$C_GREEN" "$C_BOLD" "$C_RESET"
printf '%s  %s installed successfully%s\n' "$C_GREEN" "$APP_NAME" "$C_RESET"
printf '%s================================================%s\n\n' "$C_GREEN" "$C_RESET"

printf '  Web interface : %shttp://%s/%s\n' "$C_BOLD" "${DOMAIN:-$SERVER_IP}" "$C_RESET"
printf '  Install dir   : %s\n' "$INSTALL_DIR"
printf '  Service user  : %s\n' "$SERVICE_USER"
printf '  Database      : %s/instance/ncap.db\n' "$INSTALL_DIR"
printf '  Logs          : %s   (and: journalctl -u ncap-web -f)\n' "$LOG_DIR"
printf '\n  Useful commands:\n'
printf '    systemctl status ncap-web ncap-scheduler\n'
printf '    systemctl restart ncap-web ncap-scheduler\n'
printf '    tail -f %s\n' "$LOG_DIR/ncap.log"
printf '\n  Next steps:\n'
printf '    1. Open the site and sign in as the administrator.\n'
printf '    2. Admin -> Sources -> Probe each portal to confirm it can be read.\n'
printf '    3. Admin -> Settings -> Scheduling to choose the check interval.\n'
printf '    4. (Optional) sudo certbot --nginx -d %s for HTTPS,\n' "${DOMAIN:-your-hostname}"
printf '       then set NCAP_SESSION_SECURE=true in %s and restart.\n' "$ENV_FILE"
printf '\n'

if systemctl is-active --quiet ncap-web; then
  ok "ncap-web is running"
else
  warn "ncap-web is not active - check: journalctl -u ncap-web -n 50"
fi
if systemctl is-active --quiet ncap-scheduler; then
  ok "ncap-scheduler is running"
else
  warn "ncap-scheduler is not active - check: journalctl -u ncap-scheduler -n 50"
fi
printf '\n'
