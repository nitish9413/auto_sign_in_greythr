# GreytHR Auto Punch-In

Logs in to GreytHR with Playwright and clicks **Sign In** if you haven't already. Sends a Windows toast and a Microsoft Teams alert with the result.

## Quick start

**Prerequisites:** [uv](https://docs.astral.sh/uv/), Git, and Git Bash (Windows) or any bash shell.

```bash
# 1. Clone
git clone <repo-url>
cd <repo-folder>

# 2. Create .env and fill in your details
cp .env.example .env
#    then open .env and set at least:
#      GREYTHR_USER=your_employee_id
#      GREYTHR_PASS=your_password
#      TEAMS_WEBHOOK_URL=...        (optional)

# 3. Run setup
bash setup.sh
```

`setup.sh` will:
1. Check `uv` is installed
2. Install dependencies (`uv sync`)
3. Install Playwright's Chromium
4. Keep your existing `.env` untouched (if `.env` is missing it creates one and asks for the details)
5. Run a dry-run login test (it never clicks Sign In)

Options: `bash setup.sh --no-prompt` (no questions) and `bash setup.sh --no-check` (skip the dry-run).

## Usage

```bash
uv run python punch.py              # normal run
uv run python punch.py --dry-run    # log in and check state, don't click
uv run python punch.py --force      # run even on weekends
uv run python punch.py --no-delay   # skip the startup delay
```

Exit codes: `0` success, `1` punch failed, `2` bad config.

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
| `STARTUP_DELAY_SECONDS` | no | `60` | Wait for network after boot/login |
| `MAX_ATTEMPTS` | no | `3` | |
| `RETRY_BACKOFF_SECONDS` | no | `20` | |
| `NAV_TIMEOUT_MS` / `ELEMENT_TIMEOUT_MS` | no | `45000` / `15000` | |
| `DATA_DIR` | no | repo folder | Where `logs/` and `screenshots/` go |
| `SCREENSHOT_RETENTION_DAYS` | no | `30` | `0` keeps forever |

## Run it daily (Windows Task Scheduler)

Create a task with a weekday trigger (e.g. at log-on) and this action:

- **Program:** `uv`
- **Arguments:** `run --directory "C:\path\to\repo" python punch.py`

## Notifications

| Situation | Toast | Teams |
|---|---|---|
| Punched in | yes | yes (green) |
| Already signed in | yes | yes (green) |
| All attempts failed | yes, with an "Open GreytHR" button | yes (red) |

## Troubleshooting

- **Logs:** `logs/punch.log` (rotates automatically)
- **Screenshots:** `screenshots/<date>/`, saved on success and on every failure (`login_failed`, `no_button`, `punch_unconfirmed`, `timeout`, `error`)
- **`Config problem: ...`:** fix `.env`, then re-run
- **`uv not found`:** install uv, reopen the terminal, re-run `setup.sh`
- **Login or button selectors stop working after a GreytHR UI change:** edit the `SEL_*` constants at the top of the punch section in `punch.py`

## Security

- `.env` is git-ignored. Never commit it.
- Treat the Teams webhook URL like a password, since it contains a signature. If it leaks, regenerate it.