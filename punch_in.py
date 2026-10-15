"""GreytHR automated punch-in.

Config comes from environment variables / a `.env` file next to the script
(or next to the .exe when frozen with PyInstaller). See `.env.example`.

Usage:
    python punch_in.py              # normal run
    python punch_in.py --dry-run    # log in and report state, never click Sign In
    python punch_in.py --force      # run even on weekends / if already done today
    python punch_in.py --no-delay   # skip the startup delay (handy for testing)
    python punch_in.py --watchdog   # alert if no successful punch-in is recorded today
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeoutError

try:  # Windows-only; the script still runs elsewhere without toasts
    from win11toast import toast
except ImportError:  # pragma: no cover
    toast = None

log = logging.getLogger("greythr")

DATE_FMT = "%Y-%m-%d"
DEFAULT_LOGIN_URL = "https://kanaka-software.greythr.com/uas/portal/auth/login"


# ---------------- PATHS ----------------
def app_dir() -> Path:
    """Folder of the script, or of the .exe when built with PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
load_dotenv(APP_DIR / ".env")


# ---------------- CONFIG ----------------
def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"Config error: {name} must be an integer, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def parse_hhmm(value: str) -> dtime:
    """'10:30' -> time(10, 30). Raises ValueError on bad input."""
    return datetime.strptime(value, "%H:%M").time()


@dataclass(frozen=True)
class Config:
    login_url: str
    home_url: str
    dashboard_pattern: str
    username: str
    password: str
    teams_webhook_url: str
    chromium_path: str  # empty -> Playwright's bundled Chromium
    headless: bool
    startup_delay: int
    network_wait_max: int  # seconds to wait for the network (0 = don't wait)
    max_attempts: int
    retry_backoff: int
    followup_retries: int  # extra rounds after a fully failed round (0 = off)
    followup_interval_min: int
    watchdog_deadline: str  # "HH:MM"
    reuse_session: bool
    nav_timeout_ms: int
    element_timeout_ms: int
    data_dir: Path
    retention_days: int

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "punch.log"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def session_file(self) -> Path:
        return self.data_dir / "session.json"

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_env("DATA_DIR") or APP_DIR).expanduser()
        login_url = _env("GREYTHR_LOGIN_URL", DEFAULT_LOGIN_URL)
        parsed = urlparse(login_url)
        return cls(
            login_url=login_url,
            home_url=_env("GREYTHR_HOME_URL")
            or f"{parsed.scheme}://{parsed.netloc}/v3/portal/ess/home",
            dashboard_pattern=_env("GREYTHR_DASHBOARD_PATTERN", "**/v3/portal/ess/home**"),
            username=_env("GREYTHR_USER"),
            password=_env("GREYTHR_PASS"),
            teams_webhook_url=_env("TEAMS_WEBHOOK_URL"),
            chromium_path=_env("CHROMIUM_PATH"),
            headless=_env_bool("HEADLESS", True),
            startup_delay=_env_int("STARTUP_DELAY_SECONDS", 10),
            network_wait_max=_env_int("NETWORK_WAIT_MAX_SECONDS", 300),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
            retry_backoff=_env_int("RETRY_BACKOFF_SECONDS", 20),
            followup_retries=_env_int("FOLLOWUP_RETRIES", 3),
            followup_interval_min=_env_int("FOLLOWUP_INTERVAL_MINUTES", 15),
            watchdog_deadline=_env("WATCHDOG_DEADLINE", "10:30"),
            reuse_session=_env_bool("SESSION_REUSE", True),
            nav_timeout_ms=_env_int("NAV_TIMEOUT_MS", 45000),
            element_timeout_ms=_env_int("ELEMENT_TIMEOUT_MS", 15000),
            data_dir=data_dir,
            retention_days=_env_int("SCREENSHOT_RETENTION_DAYS", 30),
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable config problems (empty = OK)."""
        problems = []
        if not self.username:
            problems.append("GREYTHR_USER is not set")
        if not self.password:
            problems.append("GREYTHR_PASS is not set")
        if self.chromium_path and not Path(self.chromium_path).is_file():
            problems.append(f"CHROMIUM_PATH does not exist: {self.chromium_path}")
        if self.max_attempts < 1:
            problems.append("MAX_ATTEMPTS must be at least 1")
        try:
            parse_hhmm(self.watchdog_deadline)
        except ValueError:
            problems.append(f"WATCHDOG_DEADLINE must be HH:MM, got {self.watchdog_deadline!r}")
        return problems


# ---------------- LOGGING ----------------
def setup_logging(cfg: Config) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()  # avoid duplicate handlers on re-entry

    file_handler = RotatingFileHandler(
        cfg.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # A windowed (--noconsole) exe has no stderr, so only log to the console when one exists.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)


# ---------------- STATE (shared by the punch run and the watchdog) ----------------
def load_state(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(cfg: Config, **updates) -> None:
    state = load_state(cfg)
    state.update(updates)
    try:
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(cfg.state_file)
    except OSError as e:
        log.warning("Could not save state file: %s", e)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------- NETWORK ----------------
def wait_for_network(cfg: Config) -> bool:
    """Poll until the GreytHR host accepts a TCP connection (or the wait runs out)."""
    if cfg.network_wait_max <= 0:
        return True
    parsed = urlparse(cfg.login_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    deadline = time.monotonic() + cfg.network_wait_max
    log.info("Waiting for network (up to %ds)", cfg.network_wait_max)
    while True:
        try:
            socket.create_connection((host, port), timeout=5).close()
            log.info("Network is up")
            return True
        except OSError:
            if time.monotonic() >= deadline:
                log.warning("Network still not reachable after %ds; trying anyway", cfg.network_wait_max)
                return False
            time.sleep(5)


# ---------------- SCREENSHOTS ----------------
class Screenshots:
    def __init__(self, base: Path, retention_days: int):
        self.base = base
        self.retention_days = retention_days

    def save(self, page: Page, tag: str = "punch") -> Path | None:
        now = datetime.now()
        day_dir = self.base / now.strftime(DATE_FMT)
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{tag}_{now:%H-%M-%S}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as e:  # page/browser may already be gone
            log.warning("Could not save screenshot (%s): %s", tag, e)
            return None

    def cleanup(self) -> None:
        """Delete day-folders older than the retention window (0 = keep all)."""
        if self.retention_days <= 0 or not self.base.exists():
            return
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        for d in self.base.iterdir():
            try:
                if d.is_dir() and datetime.strptime(d.name, DATE_FMT) < cutoff:
                    shutil.rmtree(d)
                    log.info("Removed old screenshots: %s", d.name)
            except ValueError:
                continue  # not a date folder, leave it alone
            except OSError as e:
                log.warning("Could not remove %s: %s", d, e)


# ---------------- NOTIFICATIONS ----------------
class Notifier:
    CARD_COLORS = {"success": "Good", "failure": "Attention", "warning": "Warning"}

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- Teams --
    def teams(self, status: str, title: str, details: str, attempt: int | None = None) -> None:
        if not self.cfg.teams_webhook_url:
            log.info("TEAMS_WEBHOOK_URL not set; skipping Teams alert.")
            return

        facts = [
            {"title": "User:", "value": self.cfg.username or "-"},
            {"title": "Time:", "value": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
        ]
        if attempt is not None:
            facts.append({"title": "Attempt:", "value": f"{attempt}/{self.cfg.max_attempts}"})

        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "size": "Medium",
                                "weight": "Bolder",
                                "text": title,
                                "color": self.CARD_COLORS.get(status, "Default"),
                            },
                            {"type": "FactSet", "facts": facts},
                            {"type": "TextBlock", "text": details, "wrap": True},
                        ],
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "Open GreytHR Portal",
                                "url": self.cfg.login_url,
                            }
                        ],
                    },
                }
            ],
        }
        try:
            req = urllib.request.Request(
                self.cfg.teams_webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 202):
                    log.info("Teams notification sent.")
                else:
                    log.warning("Teams notification returned status %s", resp.status)
        except Exception as e:  # never log the URL: it contains a secret signature
            log.error("Failed to post Teams notification: %s", type(e).__name__)

    # -- Windows toast --
    def toast(self, title: str, message: str, ok: bool = True, with_button: bool = False) -> None:
        if toast is None:
            return
        icon = "checkmark" if ok else "error"
        kwargs = {
            "icon": {
                "src": f"https://img.icons8.com/color/96/{icon}.png",
                "placement": "appLogoOverride",
            }
        }
        if ok:
            kwargs["duration"] = "short"
        if with_button:
            kwargs["buttons"] = ["Open GreytHR"]
            kwargs["on_click"] = (
                lambda args: os.startfile(self.cfg.login_url)
                if args.get("arguments") == "Open GreytHR"
                else None
            )
        try:
            toast(title, message, **kwargs)
        except Exception as e:
            log.error("Could not show toast: %s", e)


# ---------------- PUNCH LOGIC ----------------
SEL_USER = "input[type='text'], input[type='email']"
SEL_PASS = "input[type='password']"
SEL_LOGIN_BTN = "button:has-text('Login'), button:has-text('Sign In')"
# GreytHR renders BOTH buttons and disables the inactive one, so only match enabled ones.
SEL_SIGN_IN = "button:has-text('Sign In'):not([disabled])"
SEL_SIGN_OUT = "button:has-text('Sign Out'):not([disabled])"


class Result(Enum):
    PUNCHED = "punched"
    ALREADY_IN = "already_in"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class PunchError(Exception):
    """A step failed; `tag` names the failure screenshot."""

    def __init__(self, tag: str, message: str):
        super().__init__(message)
        self.tag = tag


def login(page: Page, cfg: Config) -> None:
    page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
    # fill() auto-waits for the element to be visible/enabled
    page.locator(SEL_USER).first.fill(cfg.username)
    page.locator(SEL_PASS).first.fill(cfg.password)
    page.locator(SEL_LOGIN_BTN).first.click()
    try:
        page.wait_for_url(cfg.dashboard_pattern, timeout=cfg.nav_timeout_ms)
    except PWTimeoutError:
        raise PunchError("login_failed", f"Never reached dashboard. URL: {page.url}")


def try_saved_session(page: Page, cfg: Config) -> bool:
    """Open the dashboard directly. True = still logged in, False = need to log in."""
    try:
        page.goto(cfg.home_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        # Dashboard shows an enabled Sign In/Out button; an expired session shows the
        # login form instead. (A password field only exists on the login page.)
        page.locator(f"{SEL_SIGN_IN}, {SEL_SIGN_OUT}, {SEL_PASS}").first.wait_for(
            state="visible", timeout=cfg.nav_timeout_ms
        )
        return not page.locator(SEL_PASS).first.is_visible()
    except PWTimeoutError:
        return False


def save_session(context: BrowserContext, cfg: Config) -> None:
    if not cfg.reuse_session:
        return
    try:
        cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cfg.session_file))
    except Exception as e:
        log.warning("Could not save session: %s", e)


def punch(page: Page, cfg: Config, shots: Screenshots, dry_run: bool) -> Result:
    sign_in = page.locator(SEL_SIGN_IN).first
    sign_out = page.locator(SEL_SIGN_OUT).first

    try:  # wait until either button is enabled (comma selector + .first = no strict-mode error)
        page.locator(f"{SEL_SIGN_IN}, {SEL_SIGN_OUT}").first.wait_for(
            state="visible", timeout=cfg.nav_timeout_ms
        )
    except PWTimeoutError:
        raise PunchError("no_button", "Neither Sign In nor Sign Out is enabled")

    if sign_out.is_visible():
        log.info("Already punched in (Sign Out visible)")
        shots.save(page)
        return Result.ALREADY_IN

    if dry_run:
        log.info("[dry-run] Sign In button is visible; not clicking.")
        return Result.DRY_RUN

    log.info("Clicking Sign In")
    sign_in.click()
    try:
        sign_out.wait_for(state="visible", timeout=cfg.element_timeout_ms)
    except PWTimeoutError:
        raise PunchError("punch_unconfirmed", "Clicked Sign In but state did not change")

    log.info("Punch confirmed (Sign Out now visible)")
    shots.save(page)
    return Result.PUNCHED


def attempt_punch(
    browser: Browser, cfg: Config, shots: Screenshots, attempt: int, dry_run: bool
) -> Result:
    prefix = f"[Attempt {attempt}]"
    saved = cfg.reuse_session and cfg.session_file.is_file()
    try:
        context = (
            browser.new_context(storage_state=str(cfg.session_file))
            if saved
            else browser.new_context()
        )
    except Exception as e:  # corrupt/unreadable session file
        log.warning("%s Ignoring unreadable session file: %s", prefix, e)
        saved = False
        context = browser.new_context()

    page = context.new_page()
    page.set_default_timeout(cfg.element_timeout_ms)
    try:
        if saved and try_saved_session(page, cfg):
            log.info("%s Reused saved session (login skipped)", prefix)
        else:
            log.info(
                "%s %s",
                prefix,
                "Saved session expired; logging in again" if saved else "Opening login page",
            )
            login(page, cfg)
        log.info("%s Dashboard loaded: %s", prefix, page.url)
        save_session(context, cfg)
        return punch(page, cfg, shots, dry_run)
    except PunchError as e:
        path = shots.save(page, e.tag)
        log.warning("%s %s. Screenshot: %s", prefix, e, path)
    except PWTimeoutError as e:
        path = shots.save(page, "timeout")
        log.error("%s Timeout: %s. Screenshot: %s", prefix, e, path)
    except Exception:
        path = shots.save(page, "error")
        log.exception("%s Unexpected error. Screenshot: %s", prefix, path)
    finally:
        context.close()
    return Result.FAILED


def run_round(cfg: Config, shots: Screenshots, dry_run: bool) -> tuple[Result, int]:
    """One full round: launch the browser, try up to MAX_ATTEMPTS times, close it."""
    launch_kwargs = {"headless": cfg.headless}
    if cfg.chromium_path:
        launch_kwargs["executable_path"] = cfg.chromium_path

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                for attempt in range(1, cfg.max_attempts + 1):
                    result = attempt_punch(browser, cfg, shots, attempt, dry_run)
                    if result is not Result.FAILED:
                        return result, attempt
                    if attempt < cfg.max_attempts:
                        log.info(
                            "Retrying in %ds (attempt %d/%d)",
                            cfg.retry_backoff, attempt + 1, cfg.max_attempts,
                        )
                        time.sleep(cfg.retry_backoff)
            finally:
                browser.close()
    except Exception:
        log.exception("Browser failed to start (is Chromium installed? check CHROMIUM_PATH)")
    return Result.FAILED, cfg.max_attempts


# ---------------- ENTRY POINTS ----------------
def run(cfg: Config, dry_run: bool) -> bool:
    shots = Screenshots(cfg.screenshot_dir, cfg.retention_days)
    notifier = Notifier(cfg)
    shots.cleanup()

    rounds = 1 if dry_run else 1 + max(0, cfg.followup_retries)
    for round_no in range(1, rounds + 1):
        if round_no > 1:
            log.info("Follow-up round %d/%d", round_no - 1, rounds - 1)
        result, attempt = run_round(cfg, shots, dry_run)

        if result is Result.DRY_RUN:
            return True

        if result in (Result.PUNCHED, Result.ALREADY_IN):
            log.info("Punch-in OK on attempt %d (%s)", attempt, result.value)
            save_state(
                cfg,
                last_success=datetime.now().strftime(DATE_FMT),
                last_success_time=now_iso(),
                last_result=result.value,
            )
            # One title + message, shared by the toast and the Teams card
            if result is Result.PUNCHED:
                title = "GreytHR Punch-In Successful"
                message = f"Punched in successfully (attempt {attempt}/{cfg.max_attempts})."
                if round_no > 1:
                    message += f" It worked on follow-up retry {round_no - 1}."
            else:
                title = "GreytHR: Already Signed In"
                message = "You are already signed in today. No action was needed."
            notifier.toast(title, message)
            notifier.teams("success", title, message, attempt)
            return True

        if round_no < rounds:
            minutes = cfg.followup_interval_min
            log.warning(
                "All %d attempts failed. Follow-up retry %d/%d in %d min.",
                cfg.max_attempts, round_no, rounds - 1, minutes,
            )
            notifier.teams(
                "warning",
                "GreytHR Punch-In Failed - Retrying",
                f"All {cfg.max_attempts} attempts failed. Retrying automatically in "
                f"{minutes} minutes (follow-up {round_no}/{rounds - 1}).",
                cfg.max_attempts,
            )
            time.sleep(minutes * 60)
            wait_for_network(cfg)

    total = cfg.max_attempts * rounds
    log.error("All %d attempts failed over %d round(s). Punch-in NOT confirmed.", total, rounds)
    save_state(cfg, last_failure=now_iso())
    notifier.toast(
        "GreytHR punch-in failed",
        f"Automatic punch-in failed after {total} attempts.\n\n"
        f"Please sign in manually.\n\nScreenshots:\n{cfg.screenshot_dir}",
        ok=False,
        with_button=True,
    )
    notifier.teams(
        "failure",
        "GreytHR Punch-In FAILED",
        f"All {total} automated attempts failed over {rounds} round(s). "
        "Check logs/screenshots on the local machine and punch in manually.",
        cfg.max_attempts,
    )
    return False


def run_watchdog(cfg: Config, force: bool) -> int:
    """Alert if no successful punch-in is recorded today after WATCHDOG_DEADLINE."""
    now = datetime.now()
    today = now.strftime(DATE_FMT)

    if now.weekday() >= 5 and not force:
        log.info("[watchdog] Weekend. Nothing to check.")
        return 0
    try:
        deadline = datetime.combine(now.date(), parse_hhmm(cfg.watchdog_deadline))
    except ValueError:
        log.error("[watchdog] WATCHDOG_DEADLINE must be HH:MM, got %r", cfg.watchdog_deadline)
        return 2
    if now < deadline and not force:
        log.info("[watchdog] Too early (deadline %s). Nothing to do.", cfg.watchdog_deadline)
        return 0

    state = load_state(cfg)
    if state.get("last_success") == today:
        log.info("[watchdog] OK: success recorded today at %s", state.get("last_success_time"))
        return 0
    if state.get("watchdog_alerted") == today and not force:
        log.info("[watchdog] Already alerted today.")
        return 1

    last_failure = str(state.get("last_failure", ""))
    if last_failure.startswith(today):
        detail = f"The punch-in failed at {last_failure} and was not recovered."
    else:
        detail = (
            "No punch-in run was recorded today. The laptop may have been off, "
            "or the startup task did not run."
        )
    title = f"GreytHR Watchdog: No Punch-In by {cfg.watchdog_deadline}"
    log.error("[watchdog] %s", detail)

    notifier = Notifier(cfg)
    notifier.toast(title, f"{detail}\n\nPlease sign in manually.", ok=False, with_button=True)
    notifier.teams("failure", title, f"{detail} Please sign in manually.")
    save_state(cfg, watchdog_alerted=today)
    return 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="GreytHR automated punch-in")
    ap.add_argument("--dry-run", action="store_true", help="log in and check state, don't click")
    ap.add_argument("--force", action="store_true", help="run even on weekends / if already done today")
    ap.add_argument("--no-delay", action="store_true", help="skip the startup delay")
    ap.add_argument(
        "--watchdog", action="store_true",
        help="alert if no successful punch-in is recorded today (run from a scheduled task)",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config.from_env()
    setup_logging(cfg)

    if args.watchdog:
        return run_watchdog(cfg, args.force)

    if datetime.today().weekday() >= 5 and not args.force:
        log.info("Weekend detected. Skipping punch.")
        return 0

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        log.error("Fix your .env (see .env.example). Exiting.")
        return 2

    state = load_state(cfg)
    if state.get("last_success") == datetime.now().strftime(DATE_FMT) and not (
        args.force or args.dry_run
    ):
        log.info(
            "Already succeeded today (%s). Nothing to do. Use --force to run anyway.",
            state.get("last_success_time"),
        )
        return 0

    if cfg.startup_delay and not (args.no_delay or args.dry_run):
        log.info("Waiting %ds for startup to settle", cfg.startup_delay)
        time.sleep(cfg.startup_delay)
    wait_for_network(cfg)

    log.info("Punch-in process started%s", " (dry-run)" if args.dry_run else "")
    return 0 if run(cfg, args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())