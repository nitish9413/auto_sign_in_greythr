"""GreytHR automated punch-in.

Config comes from environment variables / a `.env` file next to the script
(or next to the .exe when frozen with PyInstaller). See `.env.example`.

Usage:
    python punch.py              # normal run
    python punch.py --dry-run    # log in and report state, never click Sign In
    python punch.py --force      # run even on weekends
    python punch.py --no-delay   # skip the startup delay (handy for testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeoutError

try:  # Windows-only; the script still runs elsewhere without toasts
    from win11toast import toast
except ImportError:  # pragma: no cover
    toast = None

log = logging.getLogger("greythr")


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


@dataclass(frozen=True)
class Config:
    login_url: str
    dashboard_pattern: str
    username: str
    password: str
    teams_webhook_url: str
    chromium_path: str  # empty -> Playwright's bundled Chromium
    headless: bool
    startup_delay: int
    max_attempts: int
    retry_backoff: int
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

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_env("DATA_DIR") or APP_DIR).expanduser()
        return cls(
            login_url=_env(
                "GREYTHR_LOGIN_URL",
                "https://kanaka-software.greythr.com/uas/portal/auth/login",
            ),
            dashboard_pattern=_env("GREYTHR_DASHBOARD_PATTERN", "**/v3/portal/ess/home**"),
            username=_env("GREYTHR_USER"),
            password=_env("GREYTHR_PASS"),
            teams_webhook_url=_env("TEAMS_WEBHOOK_URL"),
            chromium_path=_env("CHROMIUM_PATH"),
            headless=_env_bool("HEADLESS", True),
            startup_delay=_env_int("STARTUP_DELAY_SECONDS", 60),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
            retry_backoff=_env_int("RETRY_BACKOFF_SECONDS", 20),
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
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


# ---------------- SCREENSHOTS ----------------
class Screenshots:
    DATE_FMT = "%Y-%m-%d"

    def __init__(self, base: Path, retention_days: int):
        self.base = base
        self.retention_days = retention_days

    def save(self, page: Page, tag: str = "punch") -> Path | None:
        now = datetime.now()
        day_dir = self.base / now.strftime(self.DATE_FMT)
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
                if d.is_dir() and datetime.strptime(d.name, self.DATE_FMT) < cutoff:
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
    def teams(self, status: str, title: str, details: str, attempt: int) -> None:
        if not self.cfg.teams_webhook_url:
            log.info("TEAMS_WEBHOOK_URL not set; skipping Teams alert.")
            return

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
                            {
                                "type": "FactSet",
                                "facts": [
                                    {"title": "User:", "value": self.cfg.username},
                                    {"title": "Time:", "value": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
                                    {"title": "Attempt:", "value": f"{attempt}/{self.cfg.max_attempts}"},
                                ],
                            },
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
SEL_SIGN_IN = "button:has-text('Sign In')"
SEL_SIGN_OUT = "button:has-text('Sign Out')"


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


def punch(page: Page, cfg: Config, shots: Screenshots, dry_run: bool) -> Result:
    sign_in = page.locator(SEL_SIGN_IN).first
    sign_out = page.locator(SEL_SIGN_OUT).first

    try:  # wait for whichever button shows up first
        sign_in.or_(sign_out).wait_for(state="visible", timeout=cfg.nav_timeout_ms)
    except PWTimeoutError:
        raise PunchError("no_button", "Neither Sign In nor Sign Out is visible")

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
    # Fresh context per attempt = clean cookies/session, but the browser launch is reused.
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(cfg.element_timeout_ms)
    prefix = f"[Attempt {attempt}]"
    try:
        log.info("%s Opening login page", prefix)
        login(page, cfg)
        log.info("%s Dashboard loaded: %s", prefix, page.url)
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


# ---------------- ENTRY POINT ----------------
def run(cfg: Config, dry_run: bool) -> bool:
    shots = Screenshots(cfg.screenshot_dir, cfg.retention_days)
    notifier = Notifier(cfg)
    shots.cleanup()

    launch_kwargs = {"headless": cfg.headless}
    if cfg.chromium_path:
        launch_kwargs["executable_path"] = cfg.chromium_path

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        try:
            for attempt in range(1, cfg.max_attempts + 1):
                result = attempt_punch(browser, cfg, shots, attempt, dry_run)

                if result is Result.DRY_RUN:
                    return True
                if result in (Result.PUNCHED, Result.ALREADY_IN):
                    log.info("Punch-in OK on attempt %d (%s)", attempt, result.value)
                    if result is Result.PUNCHED:
                        msg = f"Punched in successfully (attempt {attempt}/{cfg.max_attempts})"
                        title = "GreytHR Punch-In Successful"
                        detail = "Automated punch-in completed successfully."
                    else:
                        msg = "Already punched in today."
                        title = "GreytHR Already Punched In"
                        detail = "Sign Out was already visible; nothing to do."
                    notifier.toast("GreytHR punch-in", msg)
                    notifier.teams("success", title, detail, attempt)
                    return True

                if attempt < cfg.max_attempts:
                    log.info(
                        "Retrying in %ds (attempt %d/%d)",
                        cfg.retry_backoff, attempt + 1, cfg.max_attempts,
                    )
                    time.sleep(cfg.retry_backoff)
        finally:
            browser.close()

    log.error("All %d attempts failed. Punch-in NOT confirmed.", cfg.max_attempts)
    notifier.toast(
        "GreytHR punch-in failed",
        f"Automatic punch-in failed after {cfg.max_attempts} attempts.\n\n"
        f"Please sign in manually.\n\nScreenshots:\n{cfg.screenshot_dir}",
        ok=False,
        with_button=True,
    )
    notifier.teams(
        "failure",
        "GreytHR Punch-In FAILED",
        f"All {cfg.max_attempts} automated attempts failed. "
        "Check logs/screenshots on the local machine and punch in manually.",
        cfg.max_attempts,
    )
    return False


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="GreytHR automated punch-in")
    ap.add_argument("--dry-run", action="store_true", help="log in and check state, don't click")
    ap.add_argument("--force", action="store_true", help="run even on weekends")
    ap.add_argument("--no-delay", action="store_true", help="skip the startup delay")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config.from_env()
    setup_logging(cfg)

    if datetime.today().weekday() >= 5 and not args.force:
        log.info("Weekend detected. Skipping punch.")
        return 0

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        log.error("Fix your .env (see .env.example). Exiting.")
        return 2

    if cfg.startup_delay and not (args.no_delay or args.dry_run):
        log.info("Waiting %ds for network/startup to settle", cfg.startup_delay)
        time.sleep(cfg.startup_delay)

    log.info("Punch-in process started%s", " (dry-run)" if args.dry_run else "")
    return 0 if run(cfg, args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())