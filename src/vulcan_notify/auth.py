"""Authentication support for eduVULCAN."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import ssl
import subprocess
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)


logger = logging.getLogger(__name__)


KEYCHAIN_SERVICE = "vulcan-notify"

EDUVULCAN_HOME_URL = "https://eduvulcan.pl/"
EDUVULCAN_LOGIN_URL = "https://eduvulcan.pl/logowanie"
JOURNAL_ACCESS_URL = "https://eduvulcan.pl/dostep-do-dziennika/"

STUDENT_HOST = "uczen.eduvulcan.pl"
MESSAGES_HOST = "wiadomosci.eduvulcan.pl"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_bool(
    name: str,
    default: bool,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _browser_headless() -> bool:
    return _env_bool(
        "VULCAN_BROWSER_HEADLESS",
        False,
    )


def _browser_profile_dir() -> Path:
    return Path(
        os.getenv(
            "VULCAN_BROWSER_PROFILE_DIR",
            "/app/data/chromium-profile",
        )
    )


def _browser_slow_mo() -> float:
    raw = os.getenv(
        "VULCAN_BROWSER_SLOW_MO_MS",
        "0",
    )

    try:
        return max(
            0.0,
            float(raw),
        )

    except ValueError:
        return 0.0


def _get_login_delay_seconds() -> float:
    raw = os.getenv(
        "VULCAN_LOGIN_DELAY_SECONDS",
        "2",
    )

    try:
        value = float(raw)

    except ValueError:
        logger.warning(
            "Invalid VULCAN_LOGIN_DELAY_SECONDS=%r; "
            "using 2 seconds",
            raw,
        )

        return 2.0

    return max(
        0.0,
        min(value, 120.0),
    )


def _get_captcha_detect_timeout_seconds() -> float:
    raw = os.getenv(
        "VULCAN_CAPTCHA_DETECT_TIMEOUT_SECONDS",
        "5",
    )

    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _get_captcha_complete_timeout_seconds() -> float:
    raw = os.getenv(
        "VULCAN_CAPTCHA_COMPLETE_TIMEOUT_SECONDS",
        "60",
    )

    try:
        return max(1.0, float(raw))
    except ValueError:
        return 60.0

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def get_keychain_credentials() -> tuple[str, str] | None:
    """Read login/password from macOS Keychain."""

    if platform.system() != "Darwin":
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return None

        account = ""

        for line in result.stdout.splitlines():
            if '"acct"' in line and "=" in line:
                account = (
                    line.split("=", 1)[1]
                    .strip()
                    .strip('"')
                )
                break

        if not account:
            return None

        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return None

        password = result.stdout.strip()

        if not password:
            return None

        return account, password

    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _safe_url(url: str) -> str:
    try:
        parsed = urlparse(url)

        path = parsed.path

        parts = path.split("/")

        if (
            parsed.hostname == STUDENT_HOST
            and len(parts) >= 4
            and parts[2].lower() == "app"
        ):
            parts[3] = "<redacted>"
            path = "/".join(parts)

        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                path,
                "",
                "",
                "",
            )
        )

    except Exception:
        return "<invalid-url>"


def _is_student_dashboard_url(
    url: str,
) -> bool:
    try:
        parsed = urlparse(url)

        return (
            parsed.hostname == STUDENT_HOST
            and "/app" in parsed.path.lower()
        )

    except Exception:
        return False


def _tenant_from_dashboard_url(
    url: str,
) -> str:
    try:
        parsed = urlparse(url)

        if parsed.hostname != STUDENT_HOST:
            return ""

        parts = [
            p
            for p in parsed.path.split("/")
            if p
        ]

        return parts[0] if parts else ""

    except Exception:
        return ""


@contextmanager
def _browser_profile_lock(timeout: float = 30.0):
    lock_path = Path(
        os.getenv(
            "VULCAN_BROWSER_LOCK_FILE",
            "/app/data/chromium-profile.lock",
        )
    )

    lock_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handle = open(
        lock_path,
        "a+",
    )

    deadline = time.monotonic() + timeout

    while True:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            break

        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()

                raise RuntimeError(
                    "Chromium profile is currently being used "
                    "by another vulcan-notify process"
                )

            time.sleep(0.25)

    try:
        yield

    finally:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_UN,
        )

        handle.close()
        

def _cleanup_chromium_singleton_locks() -> None:
    profile_dir = _browser_profile_dir()

    for name in (
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
    ):
        path = profile_dir / name

        try:
            if path.exists() or path.is_symlink():
                logger.warning(
                    "Browser: removing stale Chromium lock %s",
                    path,
                )

                path.unlink()

        except Exception:
            logger.warning(
                "Browser: could not remove stale lock %s",
                path,
                exc_info=True,
            )
            

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def _save_screenshot(
    page: Page,
    directory: Path,
    name: str,
) -> None:
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    target = directory / f"{name}.png"

    try:
        await page.screenshot(
            path=str(target),
            full_page=True,
        )

        logger.info(
            "Diagnostic screenshot saved: %s",
            target,
        )

    except Exception:
        logger.exception(
            "Unable to save screenshot"
        )


async def _save_html(
    page: Page,
    directory: Path,
    name: str,
) -> None:
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    target = directory / f"{name}.html"

    try:
        target.write_text(
            await page.content(),
            encoding="utf-8",
        )

        logger.info(
            "Diagnostic HTML saved: %s",
            target,
        )

    except Exception:
        logger.exception(
            "Unable to save HTML"
        )


async def _log_page_text(
    page: Page,
    prefix: str,
    limit: int = 2000,
) -> None:
    try:
        text = await page.locator(
            "body"
        ).inner_text()

        text = " | ".join(
            x.strip()
            for x in text.splitlines()
            if x.strip()
        )

        logger.info(
            "%s: %s",
            prefix,
            text[:limit],
        )

    except Exception:
        logger.exception(
            "Unable to read page text"
        )


# ---------------------------------------------------------------------------
# Persistent Chromium
# ---------------------------------------------------------------------------


async def _launch_browser_context(
    playwright: Playwright,
    *,
    force_headless: bool | None = None,
) -> BrowserContext:
    """Launch Chromium using a persistent dedicated browser profile."""

    profile_dir = _browser_profile_dir()

    profile_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if force_headless is None:
        headless = _browser_headless()
    else:
        headless = force_headless

    logger.info(
        "Browser: persistent profile=%s headless=%s",
        profile_dir,
        headless,
    )

    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        slow_mo=_browser_slow_mo(),
        viewport={
            "width": 1440,
            "height": 900,
        },
        locale="pl-PL",
        args=[
            "--no-sandbox",
            "--disable-gpu",
        ],
    )


async def _seed_context_from_session(
    context: BrowserContext,
    session_path: Path,
) -> None:
    """Import cookies from existing session.json into Chromium.

    This is particularly useful for the first migration from the old
    non-persistent browser flow to the persistent Chromium profile.
    """

    if not session_path.exists():
        return

    try:
        data = json.loads(
            session_path.read_text(
                encoding="utf-8"
            )
        )

        cookies = data.get(
            "cookies",
            [],
        )

        if not cookies:
            return

        await context.add_cookies(
            cookies
        )

        logger.info(
            "Browser: imported %d cookie(s) from existing session",
            len(cookies),
        )

    except Exception:
        logger.warning(
            "Browser: could not import cookies from session.json",
            exc_info=True,
        )


def _get_page(
    context: BrowserContext,
) -> Page:
    """Return persistent context's primary page."""

    if context.pages:
        return context.pages[0]

    raise RuntimeError(
        "Persistent browser context did not create a page"
    )


# ---------------------------------------------------------------------------
# Navigation tracking
# ---------------------------------------------------------------------------


def _attach_navigation_tracking(
    context: BrowserContext,
    login_complete: asyncio.Event,
    dashboard_holder: dict[str, str],
) -> None:
    """Detect student application in current or newly opened tabs."""

    def handle_navigation(frame: Any) -> None:
        url = frame.url

        if not _is_student_dashboard_url(
            url
        ):
            return

        if not dashboard_holder.get(
            "url"
        ):
            logger.info(
                "Student dashboard detected: %s",
                _safe_url(url),
            )

        dashboard_holder["url"] = url

        login_complete.set()

    def attach_page(page: Page) -> None:
        page.on(
            "framenavigated",
            handle_navigation,
        )

        if _is_student_dashboard_url(
            page.url
        ):
            dashboard_holder["url"] = (
                page.url
            )

            login_complete.set()

    for page in context.pages:
        attach_page(page)

    context.on(
        "page",
        attach_page,
    )


async def _find_student_page(
    context: BrowserContext,
) -> Page | None:
    for page in context.pages:
        if _is_student_dashboard_url(
            page.url
        ):
            return page

    return None


# ---------------------------------------------------------------------------
# Privacy/cookies
# ---------------------------------------------------------------------------


async def _accept_cookie_banner(
    page: Page,
) -> None:
    """Accept the cookie/privacy dialog if it is actually visible.

    We intentionally prefer a real UI action instead of deleting the
    overlay so that eduVULCAN can persist its normal consent state in
    the Chromium profile.
    """

    try:
        wrapper = page.locator(
            "#respect-privacy-wrapper"
        )

        if not await wrapper.count():
            return

        if not await wrapper.is_visible():
            return

    except Exception:
        return

    logger.info(
        "Browser: privacy dialog detected"
    )

    try:
        frame = page.frame_locator(
            "#respect-privacy-frame"
        )

        for text in (
            "Akceptuję",
            "Akceptuj",
            "Zgadzam",
            "Zgadzam się",
            "OK",
            "Zamknij",
        ):
            button = frame.locator(
                f'button:has-text("{text}")'
            ).first

            try:
                await button.click(
                    timeout=3000,
                )

                logger.info(
                    "Browser: privacy dialog accepted"
                )

                await asyncio.sleep(
                    0.5
                )

                return

            except Exception:
                continue

    except Exception:
        pass

    logger.warning(
        "Browser: privacy dialog was visible but no known "
        "accept button was found"
    )


# ---------------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------------


async def _wait_for_captcha(
    page: Page,
    diagnostics_dir: Path,
) -> str | None:
    """Handle optional eduVULCAN anti-bot challenge.

    Returns:
        challenge id when CAPTCHA was present and completed,
        None when no CAPTCHA was presented.
    """

    detect_timeout = (
        _get_captcha_detect_timeout_seconds()
    )

    complete_timeout = (
        _get_captcha_complete_timeout_seconds()
    )

    captcha = page.locator(
        "#captcha"
    )

    logger.info(
        "Auto-login: checking for anti-bot challenge "
        "(detect timeout %.1fs)",
        detect_timeout,
    )

    # ---------------------------------------------------------------
    # 1. Is a CAPTCHA present at all?
    # ---------------------------------------------------------------

    try:
        await captcha.wait_for(
            state="attached",
            timeout=int(
                detect_timeout * 1000
            ),
        )

    except Exception:
        logger.info(
            "Auto-login: no anti-bot challenge detected; continuing"
        )

        return None

    # It may exist in DOM but not actually be active.
    try:
        if not await captcha.is_visible():
            logger.info(
                "Auto-login: anti-bot element present but not visible; "
                "continuing"
            )

            return None

    except Exception:
        logger.info(
            "Auto-login: anti-bot element not active; continuing"
        )

        return None

    # ---------------------------------------------------------------
    # 2. CAPTCHA exists: now wait for it to complete.
    # ---------------------------------------------------------------

    logger.info(
        "Auto-login: anti-bot challenge detected; "
        "waiting up to %.1fs for completion",
        complete_timeout,
    )

    try:
        await page.locator(
            "#captcha-success-wrapper.active"
        ).wait_for(
            state="visible",
            timeout=int(
                complete_timeout * 1000
            ),
        )

        await page.wait_for_function(
            """
            () => {
                const e =
                    document.querySelector(
                        '#captcha-response'
                    );

                return (
                    e &&
                    e.value &&
                    e.value.trim().length > 0
                );
            }
            """,
            timeout=10000,
        )

    except Exception as exc:
        logger.error(
            "Auto-login: anti-bot challenge was detected "
            "but did not complete"
        )

        await _save_screenshot(
            page,
            diagnostics_dir,
            "antibot-error",
        )

        await _save_html(
            page,
            diagnostics_dir,
            "antibot-error",
        )

        raise RuntimeError(
            "eduVULCAN anti-bot challenge was present "
            "but did not complete"
        ) from exc

    # ---------------------------------------------------------------
    # 3. Read challenge metadata for diagnostics.
    # ---------------------------------------------------------------

    try:
        response = await page.locator(
            "#captcha-response"
        ).input_value()

    except Exception:
        response = ""

    try:
        challenge = await page.locator(
            "#captcha .captcha-wrapper"
        ).get_attribute(
            "data-challenge"
        )

    except Exception:
        challenge = None

    logger.info(
        "Auto-login: anti-bot completed; "
        "challenge=%s response-length=%d",
        challenge,
        len(response),
    )

    return challenge

# ---------------------------------------------------------------------------
# Student profile
# ---------------------------------------------------------------------------


async def _open_student_profile(
    page: Page,
) -> bool:
    """Open configured a.panel-access__profile entry."""

    student_hint = (
        os.getenv(
            "VULCAN_STUDENT",
            "",
        ).strip()
        or None
    )

    profiles = page.locator(
        "a.panel-access__profile"
    )

    try:
        count = await profiles.count()

    except Exception:
        return False

    logger.info(
        "Browser: found %d journal profile(s)",
        count,
    )

    if count == 0:
        return False

    selected = None
    selected_text = ""

    if student_hint:
        hint = student_hint.lower()

        logger.info(
            "Browser: preferred student=%r",
            student_hint,
        )

        for index in range(count):
            profile = profiles.nth(
                index
            )

            try:
                text = (
                    await profile.inner_text()
                ).strip()

            except Exception:
                continue

            logger.info(
                "Browser: available profile=%r",
                text,
            )

            if hint in text.lower():
                selected = profile
                selected_text = text
                break

        if selected is None:
            logger.error(
                "Browser: profile matching %r not found",
                student_hint,
            )

            return False

    elif count == 1:
        selected = profiles.first

        try:
            selected_text = (
                await selected.inner_text()
            ).strip()

        except Exception:
            selected_text = "<unknown>"

    else:
        logger.error(
            "Browser: multiple profiles found; "
            "set VULCAN_STUDENT"
        )

        return False

    href = await selected.get_attribute(
        "href"
    )

    if not href:
        return False

    target = urljoin(
        page.url,
        href,
    )

    logger.info(
        "Browser: opening journal profile %r via %s",
        selected_text,
        _safe_url(target),
    )

    try:
        await page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        return True

    except Exception:
        # Redirect chains can interrupt page.goto() while still reaching
        # the student application.
        logger.warning(
            "Browser: journal handoff navigation interrupted; "
            "current URL=%s",
            _safe_url(page.url),
        )

        return (
            STUDENT_HOST in page.url
        )


# ---------------------------------------------------------------------------
# Reuse persistent parent portal session
# ---------------------------------------------------------------------------


async def _try_reuse_browser_session(
    page: Page,
    login_complete: asyncio.Event,
) -> bool:
    """Try to recreate the student API session without entering credentials.

    This is the main purpose of the persistent Chromium profile:
    if eduvulcan.pl still knows this browser, simply re-enter the
    selected journal and regenerate session.json.
    """

    logger.info(
        "Auto-login: trying persistent eduVULCAN browser session"
    )

    try:
        await page.goto(
            JOURNAL_ACCESS_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

    except Exception:
        logger.info(
            "Auto-login: persistent portal navigation failed"
        )

        return False

    await asyncio.sleep(
        1
    )

    if login_complete.is_set():
        logger.info(
            "Auto-login: persistent browser session reached "
            "student application directly"
        )

        return True

    parsed = urlparse(
        page.url
    )

    if (
        parsed.hostname == "eduvulcan.pl"
        and "/logowanie" in parsed.path.lower()
    ):
        logger.info(
            "Auto-login: persistent portal session requires login"
        )

        return False

    profiles = page.locator(
        "a.panel-access__profile"
    )

    try:
        count = await profiles.count()

    except Exception:
        count = 0

    if count == 0:
        logger.info(
            "Auto-login: no journal profiles available in "
            "persistent portal session"
        )

        return False

    if not await _open_student_profile(
        page
    ):
        return False

    if not login_complete.is_set():
        try:
            await asyncio.wait_for(
                login_complete.wait(),
                timeout=30,
            )

        except TimeoutError:
            return False

    logger.info(
        "Auto-login: persistent browser session successfully "
        "restored student access"
    )

    return True


# ---------------------------------------------------------------------------
# Messages application
# ---------------------------------------------------------------------------


async def _visit_messages_application(
    context: BrowserContext,
    fallback_page: Page,
    tenant: str,
) -> None:
    if not tenant:
        return

    page = await _find_student_page(
        context
    )

    if page is None:
        page = fallback_page

    logger.info(
        "Auth: establishing messages session"
    )

    try:
        await page.goto(
            f"https://{MESSAGES_HOST}/{tenant}/App",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await asyncio.sleep(
            2
        )

        logger.info(
            "Auth: messages session established"
        )

    except Exception:
        logger.warning(
            "Auth: messages application navigation "
            "did not fully complete",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Session finalization
# ---------------------------------------------------------------------------


async def _save_current_session(
    context: BrowserContext,
    page: Page,
    session_path: Path,
    dashboard_url: str,
) -> dict[str, Any]:
    tenant = _tenant_from_dashboard_url(
        dashboard_url
    )

    if not tenant:
        raise RuntimeError(
            "Could not determine eduVULCAN tenant"
        )

    await _visit_messages_application(
        context,
        page,
        tenant,
    )

    cookies = await context.cookies()

    session_data: dict[str, Any] = {
        "cookies": cookies,
        "tenant": tenant,
        "base_url": (
            f"https://{STUDENT_HOST}/{tenant}"
        ),
        "dashboard_url": dashboard_url,
    }

    session_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    session_path.write_text(
        json.dumps(
            session_data,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    logger.info(
        "Auth: session saved to %s "
        "(tenant=%s, cookies=%d)",
        session_path,
        tenant,
        len(cookies),
    )

    return session_data


# ---------------------------------------------------------------------------
# Interactive login
# ---------------------------------------------------------------------------


async def login_and_save_session(
    session_path: Path,
) -> dict[str, Any]:
    """Interactive headed login using the persistent Chromium profile."""

    login_complete = asyncio.Event()

    dashboard_holder = {
        "url": "",
    }

    with _browser_profile_lock():
        _cleanup_chromium_singleton_locks()

        async with async_playwright() as playwright:
            context = await _launch_browser_context(
                playwright,
                force_headless=False,
            )

            try:
                _attach_navigation_tracking(
                    context,
                    login_complete,
                    dashboard_holder,
                )

                await _seed_context_from_session(
                    context,
                    session_path,
                )

                page = _get_page(
                    context
                )

                # First try to use the currently valid session.json /
                # persistent browser profile without asking for login.
                if await _try_reuse_browser_session(
                    page,
                    login_complete,
                ):
                    dashboard_url = dashboard_holder[
                        "url"
                    ]

                    return await _save_current_session(
                        context,
                        page,
                        session_path,
                        dashboard_url,
                    )

                await page.goto(
                    EDUVULCAN_LOGIN_URL,
                    wait_until="domcontentloaded",
                )

                await _accept_cookie_banner(
                    page
                )

                print(
                    "[auth] Browser opened."
                )

                print(
                    "[auth] Log in normally, then open the student's "
                    "Dziennik."
                )

                try:
                    await asyncio.wait_for(
                        login_complete.wait(),
                        timeout=300,
                    )

                except TimeoutError as exc:
                    raise TimeoutError(
                        "Login timed out after 5 minutes"
                    ) from exc

                dashboard_url = dashboard_holder[
                    "url"
                ]

                await asyncio.sleep(
                    1
                )

                return await _save_current_session(
                    context,
                    page,
                    session_path,
                    dashboard_url,
                )

            finally:
                await context.close()


# ---------------------------------------------------------------------------
# Automatic login
# ---------------------------------------------------------------------------


async def auto_login(
    session_path: Path,
    login: str,
    password: str,
) -> dict[str, Any]:
    """Restore/authenticate eduVULCAN using persistent Chromium.

    Order:
      1. Open persistent Chromium profile.
      2. Import any remaining cookies from session.json.
      3. Try to enter Dziennik WITHOUT credentials.
      4. Only if that fails, perform normal login/password flow.
    """

    diagnostics_dir = (
        session_path.parent
    )

    login_complete = asyncio.Event()

    dashboard_holder = {
        "url": "",
    }

    with _browser_profile_lock():
        _cleanup_chromium_singleton_locks()

        async with async_playwright() as playwright:
            context = await _launch_browser_context(
                playwright
            )

            try:
                _attach_navigation_tracking(
                    context,
                    login_complete,
                    dashboard_holder,
                )

                # Even if test_session() says the student API session has
                # expired, some portal cookies in session.json may still be
                # useful for refreshing access.
                await _seed_context_from_session(
                    context,
                    session_path,
                )

                page = _get_page(
                    context
                )

                # -----------------------------------------------------------
                # 1. Try persistent profile first.
                # -----------------------------------------------------------

                if await _try_reuse_browser_session(
                    page,
                    login_complete,
                ):
                    dashboard_url = dashboard_holder[
                        "url"
                    ]

                    return await _save_current_session(
                        context,
                        page,
                        session_path,
                        dashboard_url,
                    )

                # -----------------------------------------------------------
                # 2. Full credentials login.
                # -----------------------------------------------------------

                logger.info(
                    "Auto-login: persistent portal session unavailable; "
                    "performing full login"
                )

                await page.goto(
                    EDUVULCAN_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                await _accept_cookie_banner(
                    page
                )

                # Username.
                login_field = page.locator(
                    "#UserName"
                )

                if not await login_field.count():
                    login_field = page.locator(
                        (
                            'input[type="text"], '
                            'input[name="UserName"], '
                            'input[type="email"]'
                        )
                    ).first

                await login_field.wait_for(
                    state="visible",
                    timeout=10000,
                )

                await login_field.fill(
                    login
                )

                next_button = page.locator(
                    "#btNext"
                )

                if not await next_button.count():
                    next_button = page.locator(
                        'button:has-text("Dalej")'
                    ).first

                await next_button.click(
                    timeout=10000,
                )

                # Password.
                password_field = page.locator(
                    "#Password"
                )

                if not await password_field.count():
                    password_field = page.locator(
                        'input[type="password"]'
                    ).first

                await password_field.wait_for(
                    state="visible",
                    timeout=15000,
                )

                await password_field.fill(
                    password
                )

                challenge_before = (
                    await _wait_for_captcha(
                        page,
                        diagnostics_dir,
                    )
                )

                delay = (
                    _get_login_delay_seconds()
                )

                if delay > 0:
                    logger.info(
                        "Auto-login: waiting %.1f seconds "
                        "before submitting login",
                        delay,
                    )

                    await asyncio.sleep(
                        delay
                    )

                login_button = page.locator(
                    "#btLogOn"
                )

                if not await login_button.count():
                    login_button = page.locator(
                        'button:has-text("Zaloguj")'
                    ).first

                await login_button.wait_for(
                    state="visible",
                    timeout=10000,
                )

                logger.info(
                    "Auto-login: submitting login form"
                )

                try:
                    async with page.expect_response(
                        lambda response: (
                            response.request.method.upper()
                            == "POST"
                            and "/logowanie"
                            in response.url.lower()
                        ),
                        timeout=15000,
                    ) as response_info:

                        await login_button.click(
                            timeout=10000,
                        )

                    login_response = (
                        await response_info.value
                    )

                    logger.info(
                        "Auto-login: login POST response "
                        "status=%d url=%s",
                        login_response.status,
                        _safe_url(
                            login_response.url
                        ),
                    )

                except Exception as exc:
                    await _save_screenshot(
                        page,
                        diagnostics_dir,
                        "login-post-error",
                    )

                    raise RuntimeError(
                        "No login POST response observed"
                    ) from exc

                try:
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=15000,
                    )

                except Exception:
                    pass

                await asyncio.sleep(
                    1
                )

                # Server rejected full login.
                if "/logowanie" in urlparse(
                    page.url
                ).path.lower():

                    challenge_after = None

                    try:
                        challenge_after = (
                            await page.locator(
                                "#captcha .captcha-wrapper"
                            ).get_attribute(
                                "data-challenge"
                            )
                        )

                    except Exception:
                        pass

                    logger.error(
                        "Auto-login: server returned login page "
                        "(challenge before=%s after=%s)",
                        challenge_before,
                        challenge_after,
                    )

                    await _log_page_text(
                        page,
                        "Login response",
                    )

                    await _save_screenshot(
                        page,
                        diagnostics_dir,
                        "login-post-rejected",
                    )

                    raise RuntimeError(
                        "eduVULCAN rejected the automated "
                        "credential login"
                    )

                logger.info(
                    "Auto-login: credentials accepted; URL=%s",
                    _safe_url(
                        page.url
                    ),
                )

                # -----------------------------------------------------------
                # 3. Enter configured student journal.
                # -----------------------------------------------------------

                if not login_complete.is_set():
                    await page.goto(
                        JOURNAL_ACCESS_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    await asyncio.sleep(
                        1
                    )

                if not login_complete.is_set():
                    if not await _open_student_profile(
                        page
                    ):
                        await _save_screenshot(
                            page,
                            diagnostics_dir,
                            "journal-profile-error",
                        )

                        raise RuntimeError(
                            "Could not find configured "
                            "journal profile"
                        )

                if not login_complete.is_set():
                    try:
                        await asyncio.wait_for(
                            login_complete.wait(),
                            timeout=30,
                        )

                    except TimeoutError as exc:
                        await _save_screenshot(
                            page,
                            diagnostics_dir,
                            "student-redirect-error",
                        )

                        raise TimeoutError(
                            "Timed out waiting for "
                            "uczen.eduvulcan.pl"
                        ) from exc

                dashboard_url = dashboard_holder[
                    "url"
                ]

                return await _save_current_session(
                    context,
                    page,
                    session_path,
                    dashboard_url,
                )

            finally:
                # launch_persistent_context() returns a BrowserContext.
                # Closing it also closes Chromium and flushes profile state.
                await context.close()


# ---------------------------------------------------------------------------
# Session file
# ---------------------------------------------------------------------------


def load_session(
    session_path: Path,
) -> dict[str, Any]:
    if not session_path.exists():
        raise FileNotFoundError(
            f"No session file at {session_path}. "
            "Run 'vulcan-notify auth' first."
        )

    return json.loads(
        session_path.read_text(
            encoding="utf-8"
        )
    )


def cookies_for_url(
    session_data: dict[str, Any],
    url: str,
) -> dict[str, str]:
    parsed = urlparse(
        url
    )

    host = (
        parsed.hostname
        or ""
    ).lower()

    matching: dict[
        str,
        str,
    ] = {}

    for cookie in session_data.get(
        "cookies",
        [],
    ):
        domain = (
            cookie.get(
                "domain",
                "",
            )
            .lstrip(".")
            .lower()
        )

        if not domain:
            continue

        if (
            host == domain
            or host.endswith(
                "." + domain
            )
        ):
            matching[
                cookie["name"]
            ] = cookie[
                "value"
            ]

    return matching


# ---------------------------------------------------------------------------
# Session validation
# ---------------------------------------------------------------------------


def _make_ssl_context() -> ssl.SSLContext:
    import certifi

    return ssl.create_default_context(
        cafile=certifi.where()
    )


async def test_session(
    session_data: dict[str, Any],
) -> bool:
    """Test the short-lived API session."""

    base_url = session_data[
        "base_url"
    ]

    url = (
        f"{base_url}/api/Context"
    )

    cookie_header = "; ".join(
        f"{k}={v}"
        for k, v in cookies_for_url(
            session_data,
            url,
        ).items()
    )

    headers = {
        "Cookie": cookie_header,
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 "
            "Safari/537.36"
        ),
        "Accept": (
            "application/json, "
            "text/plain, */*"
        ),
        "Accept-Language": (
            "pl-PL,pl;q=0.9,"
            "en-US;q=0.8,en;q=0.7"
        ),
    }

    ssl_context = (
        _make_ssl_context()
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                ssl=ssl_context,
                headers=headers,
                allow_redirects=True,
            ) as response:

                text = (
                    await response.text()
                )

                content_type = (
                    response.headers.get(
                        "content-type",
                        "",
                    )
                )

                logger.debug(
                    "Session test: status=%d "
                    "content-type=%s len=%d",
                    response.status,
                    content_type,
                    len(text),
                )

                if response.status != 200:
                    return False

                if (
                    "text/html"
                    in content_type.lower()
                ):
                    return False

                try:
                    json.loads(
                        text
                    )

                    return True

                except json.JSONDecodeError:
                    return False

    except Exception:
        logger.exception(
            "Session validation failed"
        )

        return False
