#!/usr/bin/env bash
# One-shot setup for the GreytHR punch-in script.
# Run from Git Bash / WSL / Linux / macOS:   bash setup.sh
#
# Flags:
#   --no-prompt   don't ask for credentials, just create .env from .env.example
#   --no-check    skip the final dry-run login test
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PROMPT=1
CHECK=1
for arg in "$@"; do
  case "$arg" in
    --no-prompt) PROMPT=0 ;;
    --no-check)  CHECK=0 ;;
    -h|--help)   sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. uv ----------------------------------------------------------------------
info "Checking uv"
if ! command -v uv >/dev/null 2>&1; then
  die "uv not found. Install it first:
    Windows (PowerShell): powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"
    Linux/macOS:          curl -LsSf https://astral.sh/uv/install.sh | sh
  Then reopen the terminal and re-run this script."
fi
ok "$(uv --version)"

[ -f pyproject.toml ] || die "pyproject.toml not found in $(pwd)"

# 2. Python deps -------------------------------------------------------------
info "Installing dependencies (uv sync)"
uv sync
ok "virtualenv ready"

# 3. Playwright browser ------------------------------------------------------
info "Installing Playwright Chromium"
uv run playwright install chromium
ok "Chromium installed (used when CHROMIUM_PATH is blank)"

# 4. .env --------------------------------------------------------------------
info "Setting up .env"
if [ -f .env ]; then
  ok ".env already exists, leaving it untouched"
else
  [ -f .env.example ] || die ".env.example missing"
  cp .env.example .env
  ok "created .env from .env.example"

  if [ "$PROMPT" -eq 1 ] && [ -t 0 ]; then
    read -r -p "GreytHR username: " GH_USER
    read -r -s -p "GreytHR password (hidden): " GH_PASS; echo
    read -r -p "Teams webhook URL (Enter to skip): " GH_HOOK

    # Write values via Python so special characters in passwords/URLs are safe.
    GH_USER="$GH_USER" GH_PASS="$GH_PASS" GH_HOOK="$GH_HOOK" uv run python - <<'PY'
import os
from pathlib import Path

updates = {
    "GREYTHR_USER": os.environ["GH_USER"],
    "GREYTHR_PASS": os.environ["GH_PASS"],
    "TEAMS_WEBHOOK_URL": os.environ["GH_HOOK"],
}
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
for key, value in updates.items():
    if not value:
        continue
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
    ok "credentials written to .env"
  else
    echo "   Edit .env and set GREYTHR_USER / GREYTHR_PASS (and TEAMS_WEBHOOK_URL if you want alerts)."
  fi
fi

# 5. Verify ------------------------------------------------------------------
if [ "$CHECK" -eq 1 ]; then
  if grep -Eq '^GREYTHR_USER=.+' .env && grep -Eq '^GREYTHR_PASS=.+' .env; then
    info "Dry-run login test (will NOT click Sign In)"
    if uv run python punch.py --dry-run --force --no-delay; then
      ok "dry-run passed"
    else
      echo "   Dry-run failed. Check logs/punch.log and screenshots/ for details." >&2
    fi
  else
    info "Skipping dry-run: credentials not set in .env yet"
  fi
fi

info "Done"
cat <<'EOF'
Run it:
    uv run python punch.py              # normal run
    uv run python punch.py --dry-run    # test without clicking

Task Scheduler action (Windows):
    Program:   uv
    Arguments: run --directory "<full path to this repo>" python punch.py
EOF