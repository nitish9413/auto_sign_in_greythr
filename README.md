# GreytHR Auto Punch-In

Logs in to GreytHR with Playwright and clicks **Sign In** if you haven't already. Sends a Windows toast and a Microsoft Teams alert with the result. Runs as a windowed `.exe` (no console pop-up) from the Windows Startup folder or Task Scheduler.

## Quick start

**Prerequisites:** [uv](https://docs.astral.sh/uv/) and Git.

```bash
# 1. Clone
git clone <repo-url>
cd <repo-folder>

# 2. Create .env and fill in your details
cp .env.example .env          # PowerShell: copy .env.example .env
#    set at least GREYTHR_USER and GREYTHR_PASS
#    TEAMS_WEBHOOK_URL is optional

# 3. Run setup (pick one)
bash setup.sh                                          # Git Bash / WSL
powershell -ExecutionPolicy Bypass -File .\setup.ps1   # plain PowerShell
```

If `.env` is missing or still has the placeholder values, setup asks for your username and password first, then continues.

### What setup does
1. Checks `uv` is installed
2. Installs dependencies (`uv sync`)
3. Installs Playwright's Chromium
4. Makes sure `.env` is filled (asks you if not)
5. Runs a dry-run login test (never clicks Sign In)
6. Builds `dist\punch_in.exe` (windowed) and copies `.env` next to it

Options: `--no-check` / `--no-build` in bash, `-NoCheck` / `-NoBuild` in PowerShell.

## Usage

From source:

```bash
uv run python punch_in.py              # normal run
uv run python punch_in.py --dry-run    # log in and check state, don't click
uv run python punch_in.py --force      # run even on weekends
uv run python punch_in.py --no-delay   # skip the startup delay
```

From the exe:

```powershell
.\dist\punch_in.exe --dry-run --force --no-delay
```

Exit codes: `0` success, `1` punch failed, `2` bad config.

## Build the exe manually

```powershell
uv run pyinstaller --noconfirm --onefile --noconsole --name punch_in --collect-all playwright --hidden-import win11toast punch_in.py
copy .env dist\.env
```

- `--noconsole` means no terminal window. Errors go only to `dist\logs\punch.log`.
- The exe reads **`.env` from its own folder** (`dist\.env`), not the repo root. After editing `.env`, copy it again.
- The exe is a snapshot of the code. **Rebuild after every change** to `punch_in.py` (`.\setup.ps1 -NoCheck`).
- Chromium is not bundled. Run `uv run playwright install chromium` once on each machine, or set `CHROMIUM_PATH` in `.env`.
- PermissionError retries during the build are antivirus scanning the new exe. They are harmless.

## Run it automatically

**Startup folder (runs at logon)**
1. Press `Win + R`, type `shell:startup`, press Enter.
2. Create a shortcut to the full path of `dist\punch_in.exe` in that folder.

**Task Scheduler (more control)**
- Program: full path to `dist\punch_in.exe`
- Start in: the `dist` folder
- Trigger: weekdays, at log-on

`STARTUP_DELAY_SECONDS` in `.env` gives the network time to come up after boot.

## Configuration (`.env`)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GREYTHR_USER` | yes | | Employee ID / username |
| `GREYTHR_PASS` | yes | | Password |
| `GREYTHR_LOGIN_URL` | no | company URL | |
| `GREYTHR_DASHBOARD_PATTERN` | no | `**/v3/portal/ess/home**` | URL pattern after login |
| `TEAMS_WEBHOOK_URL` | no | blank = no Teams alerts | Power Automate Workflows / webhook URL |
| `CHROMIUM_PATH` | no | blank = Playwright's Chromium | Only set to use a specific browser |
| `HEADLESS` | no | `true` | |
| `STARTUP_DELAY_SECONDS` | no | `60` | Wait for network after boot |
| `MAX_ATTEMPTS` | no | `3` | |
| `RETRY_BACKOFF_SECONDS` | no | `20` | |
| `NAV_TIMEOUT_MS` / `ELEMENT_TIMEOUT_MS` | no | `45000` / `15000` | |
| `DATA_DIR` | no | script/exe folder | Where `logs\` and `screenshots\` go |
| `SCREENSHOT_RETENTION_DAYS` | no | `30` | `0` keeps forever |

## Notifications

| Situation | Toast | Teams |
|---|---|---|
| Punched in | yes | yes (green) |
| Already signed in | yes (`GreytHR: Already Signed In`) | yes (same text, green) |
| All attempts failed | yes, with an "Open GreytHR" button | yes (red) |

## Troubleshooting

- **Logs:** `logs\punch.log` (next to the exe, rotates automatically). Because the exe has no console, this is the first place to look.
- **Screenshots:** `screenshots\<date>\`, saved on success and on every failure (`login_failed`, `no_button`, `punch_unconfirmed`, `timeout`, `error`).
- **Nothing happens at startup:** check `punch.log` for new lines. No lines usually means `.env` isn't next to the exe, or the exe crashed. Run it from a terminal with `--dry-run --force --no-delay`.
- **`Config problem: ...`:** fix `dist\.env` and run again.
- **Terminal window shows at startup:** the exe was built without `--noconsole`. Rebuild with the command above.
- **Already signed in but it errors:** GreytHR renders both Sign In and Sign Out buttons and disables the inactive one. The script only looks at the enabled one (`SEL_SIGN_IN` / `SEL_SIGN_OUT` in `punch_in.py`). If GreytHR changes its UI, edit those selectors.
- **No toast:** `win11toast` only works on Windows. The Teams alert and log still work.
- **`uv not found`:** install uv, reopen the terminal, re-run setup.
- **SmartScreen / Defender flags the exe:** common for unsigned PyInstaller exes. Choose "More info" then "Run anyway", or exclude the `dist` folder.

## Security

- `.env` and `dist\.env` contain your password. `.env`, `dist/`, `build/`, `logs/` and `screenshots/` are git-ignored. Never commit them.
- Treat the Teams webhook URL like a password, since it contains a signature. If it leaks, regenerate it.
- Don't share the `dist` folder with anyone.