#!/usr/bin/env bash
#
# setup.sh — one-shot dependency installer for the MySQL Health Monitor
#
# Usage:
#   ./setup.sh              # interactive: system deps + venv + .env
#   ./setup.sh --no-devel   # skip -dev headers (containers)
#   ./setup.sh --skip-env   # don't create .env (use env vars directly)
#
# What it does:
#   1. Checks the OS is Linux
#   2. Installs system packages needed by matplotlib (+ python3-venv if missing)
#   3. Checks Python >= 3.9
#   4. Creates .venv/ with full isolation
#   5. Installs Python deps from requirements.txt
#   6. Smoke-tests imports (pymysql, flask, matplotlib)
#   7. Creates .env interactively (asks for DB credentials, port, etc.)
#   8. Prints next steps
#
# Re-running it is safe — existing .venv and .env are left untouched (pass
# --force-venv or --force-env to recreate).
#
set -euo pipefail

# ── helpers ──────────────────────────────────────────────────────────────────

RED='\033[0;31m'   GREEN='\033[0;32m'   YELLOW='\033[0;33m'
BLUE='\033[0;34m'  BOLD='\033[1m'       RESET='\033[0m'
CYAN='\033[0;36m'

step()  { echo -e "${BLUE}==> ${BOLD}$*${RESET}"; }
ok()    { echo -e "${GREEN}✓  $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠  $*${RESET}"; }
die()   { echo -e "${RED}✗  $*${RESET}" >&2; exit 1; }

FORCE_VENV=false
FORCE_ENV=false
SKIP_DEVEL=false
SKIP_ENV=false

for arg in "$@"; do
  case "$arg" in
    --force-venv) FORCE_VENV=true ;;
    --force-env)  FORCE_ENV=true ;;
    --no-devel)   SKIP_DEVEL=true ;;
    --skip-env)   SKIP_ENV=true ;;
    -h|--help)
      sed -n '2,/^$/s/^# \?//p' "$0"; exit 0 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. check Linux ───────────────────────────────────────────────────────────

step "Checking OS…"
KERNEL="$(uname -s)"
if [[ "$KERNEL" != Linux ]]; then
  die "This setup script targets Linux.  Detected: $KERNEL"
fi
# shellcheck source=/dev/null
. /etc/os-release 2>/dev/null || true
ok "Linux — $NAME $VERSION_ID"

# ── 2. detect package manager & install system deps ──────────────────────────

step "Checking package manager…"

PKG_MANAGER=""
SUDOCMD=""

if command -v sudo &>/dev/null && [[ "$(id -u)" -ne 0 ]]; then
  SUDOCMD="sudo"
fi

if command -v apt-get &>/dev/null; then
  PKG_MANAGER="apt"
  INSTALL_CMD="$SUDOCMD apt-get install -y -qq"

  SYS_PKGS=()
  if [[ "$SKIP_DEVEL" == false ]]; then
    SYS_PKGS+=(python3-dev libfreetype6-dev libpng-dev python3-venv)
  else
    SYS_PKGS+=(libfreetype6 libpng16-16t64 python3-venv)
  fi

  step "Updating apt cache & installing system packages…"
  $SUDOCMD apt-get update -qq 2>/dev/null || warn "apt-get update had warnings (continuing)"

elif command -v dnf &>/dev/null; then
  PKG_MANAGER="dnf"
  INSTALL_CMD="$SUDOCMD dnf install -y -q"

  if [[ "$SKIP_DEVEL" == false ]]; then
    SYS_PKGS=(python3-devel freetype-devel libpng-devel)
  else
    SYS_PKGS=(freetype libpng)
  fi

  step "Installing system packages via dnf…"

elif command -v pacman &>/dev/null; then
  PKG_MANAGER="pacman"
  INSTALL_CMD="$SUDOCMD pacman -S --noconfirm --needed"
  SYS_PKGS=(python freetype2 libpng)
  step "Installing system packages via pacman…"

elif command -v zypper &>/dev/null; then
  PKG_MANAGER="zypper"
  INSTALL_CMD="$SUDOCMD zypper install -y"

  if [[ "$SKIP_DEVEL" == false ]]; then
    SYS_PKGS=(python3-devel freetype-devel libpng16-devel)
  else
    SYS_PKGS=(libfreetype6 libpng16-16)
  fi

  step "Installing system packages via zypper…"

else
  warn "No supported package manager found (apt/dnf/pacman/zypper)."
  warn "Install python3-dev + libfreetype + libpng + python3-venv manually, then re-run."
  PKG_MANAGER="none"
fi

if [[ "$PKG_MANAGER" != "none" ]]; then
  $INSTALL_CMD "${SYS_PKGS[@]}" 2>/dev/null || {
    warn "Some system packages failed to install."
    warn "Matplotlib PNG output may not work — the monitor + dashboard don't need it."
  }
  ok "System packages checked"
fi

# ── 3. check python3 version ─────────────────────────────────────────────────

step "Checking Python…"
PYTHON3="$(command -v python3 || true)"
if [[ -z "$PYTHON3" ]]; then
  die "python3 not found — install it first"
fi

PY_VER="$("$PYTHON3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"

if (( PY_MAJOR < 3 )) || { (( PY_MAJOR == 3 )) && (( PY_MINOR < 9 )); }; then
  die "Python >= 3.9 required.  Found: $PY_VER"
fi
ok "python3 $PY_VER"

# ── 4. create .venv ──────────────────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"

if [[ -d "$VENV_DIR" ]] && [[ "$FORCE_VENV" == false ]]; then
  warn ".venv already exists — skipping (pass --force-venv to recreate)"
else
  if [[ "$FORCE_VENV" == true ]]; then
    step "Removing existing .venv…"
    rm -rf "$VENV_DIR"
  fi

  step "Creating .venv (this takes a few seconds)…"
  "$PYTHON3" -m venv "$VENV_DIR" || {
    die "venv creation failed — ensure python3-venv (apt) / python3 (pacman) is installed"
  }
  ok ".venv created"
fi

VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# ── 5. upgrade pip & install Python deps ─────────────────────────────────────

step "Upgrading pip…"
"$VENV_PY" -m pip install --upgrade pip -q

step "Installing Python dependencies from requirements.txt…"
"$VENV_PIP" install -r requirements.txt | tail -1
ok "Python dependencies installed"

# ── 6. smoke test ────────────────────────────────────────────────────────────

step "Smoke-testing imports…"

IMPORT_ERRORS=0

check_import() {
  local mod="$1" pretty="$2"
  if "$VENV_PY" -c "import $mod" 2>/dev/null; then
    ok "$pretty"
  else
    warn "$pretty — import failed (some features may be limited)"
    IMPORT_ERRORS=$((IMPORT_ERRORS + 1))
  fi
}

check_import pymysql   "pymysql"
check_import flask     "flask"
check_import matplotlib "matplotlib"

if (( IMPORT_ERRORS > 0 )); then
  warn "$IMPORT_ERRORS package(s) had import warnings — review above"
else
  ok "All packages healthy"
fi

# ── 7. create .env ───────────────────────────────────────────────────────────

ENV_FILE="$SCRIPT_DIR/.env"

if [[ "$SKIP_ENV" == true ]]; then
  step "Skipping .env creation (--skip-env)"
else
  echo
  if [[ -f "$ENV_FILE" ]] && [[ "$FORCE_ENV" == false ]]; then
    warn ".env already exists — skipping (delete it or pass --force-env to recreate)"
  else
    if [[ "$FORCE_ENV" == true ]]; then
      step "Removing existing .env…"
      rm -f "$ENV_FILE"
    fi

    step "Configuring .env"
    echo -e "  ${CYAN}Enter credentials. Press Enter to keep the default (shown in brackets).${RESET}"
    echo -e "  ${CYAN}Leave DB_PASSWORD blank and you'll be prompted to enter it hidden.${RESET}"
    echo

    # ── collect answers ──────────────────────────────────────────────────────

    prompt() {
      local var="$1" label="$2" default="$3"
      local answer
      read -r -p "  ${BOLD}${label}${RESET} [${default}]: " answer
      echo "${answer:-$default}"
    }

    prompt_secret() {
      local var="$1" label="$2" default="$3"
      local answer
      read -r -s -p "  ${BOLD}${label}${RESET}: " answer
      echo
      [[ -n "$answer" ]] && echo "$answer" || echo "$default"
    }

    DB_HOST_VAL="$(prompt       DB_HOST        "DB host                                     " "127.0.0.1")"
    DB_PORT_VAL="$(prompt       DB_PORT        "DB port                                     " "3306")"
    DB_USER_VAL="$(prompt       DB_USER        "DB user                                     " "root")"
    DB_PASS_VAL="$(prompt_secret DB_PASSWORD   "DB password                                 " "password")"
    DB_NAME_VAL="$(prompt       DB_NAME        "DB name                                     " "shiksha-core-module")"
    echo
    SAMPLE_VAL="$(prompt        SAMPLE_INTERVAL "Sample interval (seconds)                     " "10")"
    THRESH_VAL="$(prompt        THREADS_RUNNING_THRESHOLD "Snapshot when Threads_running >" "10")"
    echo
    DASH_PORT_VAL="$(prompt     DASHBOARD_PORT "Dashboard port                              " "5000")"
    DASH_HOST_VAL="$(prompt     DASHBOARD_HOST "Dashboard bind address                      " "127.0.0.1")"

    # ── write .env ───────────────────────────────────────────────────────────

    cat > "$ENV_FILE" <<EOF
# MySQL Health Monitor — configuration
# Generated by setup.sh on $(date '+%Y-%m-%d %H:%M:%S')
#
# Edit this file at any time; every value is picked up on the next run.
# Lines starting with # are comments.

# ── database connection ──────────────────────────────────────────────────
DB_HOST=$DB_HOST_VAL
DB_PORT=$DB_PORT_VAL
DB_USER=$DB_USER_VAL
DB_PASSWORD=$DB_PASS_VAL
DB_NAME=$DB_NAME_VAL
DB_CONNECT_TIMEOUT=5
DB_READ_TIMEOUT=10

# ── sampling ─────────────────────────────────────────────────────────────
SAMPLE_INTERVAL=$SAMPLE_VAL

# ── thresholds ───────────────────────────────────────────────────────────
THREADS_RUNNING_THRESHOLD=$THRESH_VAL
SLOW_QUERY_SECONDS=5

# ── storage ──────────────────────────────────────────────────────────────
RETENTION_DAYS=30

# ── dashboard ────────────────────────────────────────────────────────────
DASHBOARD_HOST=$DASH_HOST_VAL
DASHBOARD_PORT=$DASH_PORT_VAL
DASHBOARD_HISTORY_ROWS=500
EOF

    chmod 600 "$ENV_FILE"
    echo
    ok ".env written ($(wc -c < "$ENV_FILE") bytes, permissions 600)"
  fi
fi

# ── 8. print next steps ──────────────────────────────────────────────────────

DASH_PORT="${DASH_PORT_VAL:-5000}"
DASH_HOST="${DASH_HOST_VAL:-127.0.0.1}"

echo
echo -e "${GREEN}${BOLD}✔  Setup complete!${RESET}"
echo
echo -e "  ${BOLD}1. Review .env:${RESET}   ${YELLOW}cat .env${RESET}  (DB credentials & settings)"
echo
echo -e "  ${BOLD}2. Test with one sample:${RESET}"
echo -e "     ${YELLOW}.venv/bin/python3 monitor.py --once${RESET}"
echo
echo -e "  ${BOLD}3. Run continuously:${RESET}"
echo -e "     ${YELLOW}.venv/bin/python3 monitor.py${RESET}"
echo
echo -e "  ${BOLD}4. Launch dashboard (2nd terminal):${RESET}"
echo -e "     ${YELLOW}.venv/bin/python3 dashboard.py${RESET}"
echo -e "     → http://${DASH_HOST}:${DASH_PORT}"
echo
echo -e "  ${BOLD}5. Install as systemd service:${RESET}"
echo -e "     ${YELLOW}./install_service.sh --user${RESET}   (user scope, no sudo)"
echo -e "     ${YELLOW}./install_service.sh${RESET}          (system-wide)"
echo
