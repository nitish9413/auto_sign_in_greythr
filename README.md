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
uv run python punch_in.py --force      # run even on weekends / if already done today
uv run python punch_in.py --no-delay   # skip the startup delay
uv run python punch_in.py --watchdog   # alert if no success recorded today
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

The script waits for GreytHR to be reachable (see Resilience below), so no long fixed delay is needed.

## Resilience

**Network wait.** After a short settle delay, the script polls GreytHR until it accepts a connection (up to `NETWORK_WAIT_MAX_SECONDS`), instead of sleeping a fixed time. If the network never comes up it still tries, and the follow-up retries below take over.

**Follow-up retries.** If all `MAX_ATTEMPTS` fail, it waits `FOLLOWUP_INTERVAL_MINUTES` and runs another full round, up to `FOLLOWUP_RETRIES` times. Each failed round sends an orange "Retrying" card to Teams. The red failure alert and toast only fire after every round has failed. The process stays alive (hidden) while it waits.

**Once per day.** After a success (or "already signed in"), the result is saved in `state.json`. Later launches the same day exit immediately. `--force` overrides that.

**Session reuse.** After a successful login the browser session is saved to `session.json`. Next run it opens the dashboard directly. If the session has expired, or the file is unreadable, it logs in normally. Set `SESSION_REUSE=false` to turn this off. `session.json` holds login cookies, so protect it like a password.

**Watchdog.** `punch_in.exe --watchdog` reads `state.json` and, if there is no success recorded today after `WATCHDOG_DEADLINE`, sends a toast and a Teams alert (once per day). It also catches runs that never happened (laptop off, startup task not firing). Skips weekends. Register it as a scheduled task (PowerShell, from the repo folder):

```powershell
$exe      = (Resolve-Path .\dist\punch_in.exe).Path
$action   = New-ScheduledTaskAction -Execute $exe -Argument "--watchdog" -WorkingDirectory (Split-Path $exe)
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 10:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "GreytHR Watchdog" -Action $action -Trigger $trigger -Settings $settings -Force
```

Keep the trigger time at or after `WATCHDOG_DEADLINE`. `-StartWhenAvailable` runs it when the laptop wakes after a missed time. If the laptop is completely off at 10:30, nothing can run on it, so the alert comes late or not at all.

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
| `STARTUP_DELAY_SECONDS` | no | `10` | Short settle time after boot |
| `NETWORK_WAIT_MAX_SECONDS` | no | `300` | Wait until GreytHR is reachable (`0` = don't wait) |
| `FOLLOWUP_RETRIES` | no | `3` | Extra rounds after a fully failed round (`0` = off) |
| `FOLLOWUP_INTERVAL_MINUTES` | no | `15` | Gap between follow-up rounds |
| `WATCHDOG_DEADLINE` | no | `10:30` | `--watchdog` alerts if no success by this time |
| `SESSION_REUSE` | no | `true` | Reuse the saved login session |
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
- **Nothing runs, log says "Already succeeded today":** `state.json` has today's success. Use `--force`, or delete `state.json`.
- **Login keeps being skipped but punch fails:** delete `session.json` to force a fresh login.
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