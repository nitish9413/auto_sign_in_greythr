import os
import time
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from win11toast import toast


def show_alert_popup(title, message):
    try:
        toast(
            title,
            message,
            buttons=["Open GreytHR"],
            on_click=lambda args: os.startfile(LOGIN_URL)
            if args.get("arguments") == "Open GreytHR"
            else None,
            icon={
                "src": "https://img.icons8.com/color/96/error.png",
                "placement": "appLogoOverride",
            },
        )
    except Exception as e:
        logging.error(f"Could not show popup alert: {e}")


def show_success_toast(message):
    try:
        toast(
            "GreytHR punch-in",
            message,
            duration="short",
            icon={
                "src": "https://img.icons8.com/color/96/checkmark.png",
                "placement": "appLogoOverride",
            },
        )
    except Exception as e:
        logging.error(f"Could not show success toast: {e}")


# ---------------- CONFIG ----------------
LOGIN_URL = "https://kanaka-software.greythr.com/uas/portal/auth/login"
DASHBOARD_URL_PATTERN = (
    "**/v3/portal/ess/home**"  # trailing ** to tolerate query params
)

USERNAME = os.getenv("GREYTHR_USER", "KSCPL204")
PASSWORD = os.getenv("GREYTHR_PASS", "Umtyelshyocge@03")

STARTUP_DELAY_SECONDS = 60
MAX_ATTEMPTS = 3  # retry the whole punch flow this many times
RETRY_BACKOFF_SECONDS = 20  # wait between attempts
NAV_TIMEOUT_MS = 45000  # per-navigation timeout, generous for slow mornings
ELEMENT_TIMEOUT_MS = 15000

CHROMIUM_PATH = (
    r"C:\Users\kanaka\AppData\Local\ms-playwright\chromium-1200\chrome-win64\chrome.exe"
)

BASE_DIR = r"C:\Users\kanaka\Desktop\greythr"
LOG_DIR = os.path.join(BASE_DIR, "logs")
SCREENSHOT_BASE_DIR = os.path.join(BASE_DIR, "screenshots")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_BASE_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "punch.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging.getLogger().addHandler(logging.StreamHandler())


def is_weekday():
    return datetime.today().weekday() < 5


def get_screenshot_path(tag="punch"):
    today = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H-%M-%S")
    day_dir = os.path.join(SCREENSHOT_BASE_DIR, today)
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"{tag}_{time_str}.png")


def attempt_punch(playwright, attempt_no):
    browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM_PATH)
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(ELEMENT_TIMEOUT_MS)

    try:
        logging.info(f"[Attempt {attempt_no}] Opening login page")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

        user_field = page.locator("input[type='text'], input[type='email']").first
        pass_field = page.locator("input[type='password']").first
        user_field.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
        pass_field.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)

        user_field.fill(USERNAME)
        pass_field.fill(PASSWORD)

        login_btn = page.locator(
            "button:has-text('Login'), button:has-text('Sign In')"
        ).first
        login_btn.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
        login_btn.click()

        try:
            page.wait_for_url(DASHBOARD_URL_PATTERN, timeout=NAV_TIMEOUT_MS)
        except PWTimeoutError:
            fail_shot = get_screenshot_path("login_failed")
            page.screenshot(path=fail_shot, full_page=True)
            logging.error(
                f"[Attempt {attempt_no}] Never reached dashboard. "
                f"Current URL: {page.url}. Screenshot: {fail_shot}"
            )
            return False

        page.wait_for_selector(
            "button:has-text('Sign Out'), button:has-text('Sign In')",
            timeout=NAV_TIMEOUT_MS,
        )
        logging.info(f"[Attempt {attempt_no}] Dashboard loaded: {page.url}")

        sign_out_btn = page.locator("button:has-text('Sign Out')").first
        sign_in_btn = page.locator("button:has-text('Sign In')").first

        if sign_out_btn.is_visible():
            logging.info(
                f"[Attempt {attempt_no}] Already punched in (Sign Out visible)"
            )
            page.screenshot(path=get_screenshot_path(), full_page=True)
            return True

        if sign_in_btn.is_visible():
            logging.info(f"[Attempt {attempt_no}] Sign In button found, clicking")
            sign_in_btn.click()

            try:
                page.wait_for_selector(
                    "button:has-text('Sign Out')", timeout=ELEMENT_TIMEOUT_MS
                )
                logging.info(
                    f"[Attempt {attempt_no}] Punch confirmed (Sign Out now visible)"
                )
                page.screenshot(path=get_screenshot_path(), full_page=True)
                return True
            except PWTimeoutError:
                fail_shot = get_screenshot_path("punch_unconfirmed")
                page.screenshot(path=fail_shot, full_page=True)
                logging.warning(
                    f"[Attempt {attempt_no}] Clicked Sign In but could not "
                    f"confirm state change. Screenshot: {fail_shot}"
                )
                return False
        else:
            fail_shot = get_screenshot_path("no_button")
            page.screenshot(path=fail_shot, full_page=True)
            logging.warning(
                f"[Attempt {attempt_no}] Neither Sign In nor Sign Out "
                f"visible. Screenshot: {fail_shot}"
            )
            return False

    except PWTimeoutError as e:
        fail_shot = get_screenshot_path("timeout")
        try:
            page.screenshot(path=fail_shot, full_page=True)
        except Exception:
            pass
        logging.error(f"[Attempt {attempt_no}] Timeout: {e}. Screenshot: {fail_shot}")
        return False
    except Exception as e:
        fail_shot = get_screenshot_path("error")
        try:
            page.screenshot(path=fail_shot, full_page=True)
        except Exception:
            pass
        logging.exception(
            f"[Attempt {attempt_no}] Unexpected error. Screenshot: {fail_shot}"
        )
        return False
    finally:
        browser.close()


def main():
    if not is_weekday():
        logging.info("Weekend detected. Skipping punch.")
        return

    time.sleep(STARTUP_DELAY_SECONDS)

    if not USERNAME or USERNAME == "xx" or not PASSWORD or PASSWORD == "xxx":
        logging.error("Credentials not set (still default placeholders). Exiting.")
        return

    logging.info("Punch-in process started")

    with sync_playwright() as p:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            success = attempt_punch(p, attempt)
            if success:
                logging.info(f"Punch-in succeeded on attempt {attempt}")
                show_success_toast(
                    f"Punched in successfully (attempt {attempt}/{MAX_ATTEMPTS})"
                )
                return
            if attempt < MAX_ATTEMPTS:
                logging.info(
                    f"Retrying in {RETRY_BACKOFF_SECONDS}s "
                    f"(attempt {attempt + 1}/{MAX_ATTEMPTS})"
                )
                time.sleep(RETRY_BACKOFF_SECONDS)

    logging.error(f"All {MAX_ATTEMPTS} attempts failed. Punch-in NOT confirmed today.")
    show_alert_popup(
        "GreytHR punch-in failed",
        f"Automatic punch-in failed after {MAX_ATTEMPTS} attempts.\n\n"
        f"Please sign in manually.\n\n"
        f"Check screenshots in:\n{SCREENSHOT_BASE_DIR}",
    )


if __name__ == "__main__":
    main()
