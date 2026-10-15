#!/usr/bin/env bash
# One-shot setup + build for the GreytHR punch-in app.
# Run from Git Bash (Windows):   bash setup.sh
#
# Steps: install deps -> install Chromium -> make sure .env is filled
#        (asks you if not) -> dry-run login test -> build dist/punch_in.exe
#
# Flags:
#   --no-check   skip the dry-run login test
#   --no-build   skip building the exe
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

APP=punch_in
CHECK=1
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --no-check) CHECK=0 ;;
    --no-build) BUILD=0 ;;
    -h|--help)  sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Read KEY's value from .env (empty if missing)
env_get() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r' || true
}

# True if KEY is set to something other than empty / the .env.example placeholder
is_filled() {
  local v
  v="$(env_get "$1")"
  [ -n "$v" ] && [ "$v" != "your_employee_id" ] && [ "$v" != "your_password" ]
}

# Set KEY=VALUE in .env (via Python so special characters are safe)
env_set() {
  ENV_KEY="$1" ENV_VAL="$2" uv run python - <<'PY'
import os
from pathlib import Path

key, value = os.environ["ENV_KEY"], os.environ["ENV_VAL"]
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines):
    if line.startswith(f"{key}="):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

# 1. uv ----------------------------------------------------------------------
info "Checking uv"
if ! command -v uv >/dev/null 2>&1; then
  die "uv not found. Install it first:
    Windows (PowerShell): powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"
  Then reopen the terminal and re-run this script."
fi
ok "$(uv --version)"

[ -f pyproject.toml ] || die "pyproject.toml not found in $(pwd)"
[ -f "$APP.py" ]      || die "$APP.py not found in $(pwd)"

# 2. Python deps -------------------------------------------------------------
info "Installing dependencies (uv sync)"
uv sync
ok "virtualenv ready"

# 3. Playwright browser ------------------------------------------------------
info "Installing Playwright Chromium"
uv run playwright install chromium
ok "Chromium installed (used when CHROMIUM_PATH is blank)"

# 4. .env (must be filled before we go any further) --------------------------
info "Checking .env"
if [ ! -f .env ]; then
  [ -f .env.example ] || die ".env.example missing"
  cp .env.example .env
  ok "created .env from .env.example"
fi

if is_filled GREYTHR_USER && is_filled GREYTHR_PASS; then
  ok ".env has your GreytHR details"
else
  warn ".env is missing your GreytHR details."
  if [ ! -t 0 ]; then
    die "Open .env, fill GREYTHR_USER and GREYTHR_PASS, then re-run: bash setup.sh"
  fi

  echo "Enter your details now (saved to .env, which is git-ignored)."
  GH_USER=""
  while [ -z "$GH_USER" ]; do read -r -p "GreytHR username: " GH_USER; done
  GH_PASS=""
  while [ -z "$GH_PASS" ]; do read -r -s -p "GreytHR password (hidden): " GH_PASS; echo; done
  env_set GREYTHR_USER "$GH_USER"
  env_set GREYTHR_PASS "$GH_PASS"

  if [ -z "$(env_get TEAMS_WEBHOOK_URL)" ]; then
    read -r -p "Teams webhook URL (Enter to skip): " GH_HOOK
    [ -n "$GH_HOOK" ] && env_set TEAMS_WEBHOOK_URL "$GH_HOOK"
  fi
  ok "details saved to .env"
fi

# 5. Dry-run login test ------------------------------------------------------
if [ "$CHECK" -eq 1 ]; then
  info "Dry-run login test (will NOT click Sign In)"
  if uv run python "$APP.py" --dry-run --force --no-delay; then
    ok "dry-run passed"
  else
    warn "Dry-run failed. Check logs/punch.log and screenshots/ for details."
    if [ -t 0 ]; then
      read -r -p "Build the exe anyway? [y/N] " ANS
      [[ "$ANS" =~ ^[Yy]$ ]] || die "Stopped before build. Fix the issue and re-run."
    else
      die "Stopped before build."
    fi
  fi
fi

# 6. Build the exe -----------------------------------------------------------
if [ "$BUILD" -eq 1 ]; then
  info "Building $APP executable (takes a minute)"
  if uv run python -c "import PyInstaller" >/dev/null 2>&1; then
    PYI=(uv run pyinstaller)
  else
    PYI=(uv run --with pyinstaller pyinstaller)
  fi
  "${PYI[@]}" --noconfirm --onefile --noconsole \
    --name "$APP" \
    --collect-all playwright \
    --hidden-import win11toast \
    "$APP.py"

  mkdir -p dist
  cp .env dist/.env
  ok "built dist/$APP.exe and copied .env next to it"
  warn "dist/.env contains your password. Don't share the dist folder."
fi

info "Done"
cat <<EOF
Run it:
    uv run python $APP.py --dry-run     # test from source, no click
    dist/$APP.exe --dry-run --force --no-delay

Startup / Task Scheduler:
    Point it at the full path of dist\\$APP.exe (the .env beside it is used).
    The build is windowed, so nothing pops up; check dist\\logs\\punch.log.
    Rebuild after any code change: bash setup.sh --no-check
EOF