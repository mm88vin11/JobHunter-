"""
HH.ru OAuth via headless Playwright.
Flow: email/phone → HH sends OTP → user enters code → tokens.

Based on working implementation from the old bot project.
Key tricks:
- Stealth script to hide automation markers
- Desktop Chrome context (1280x800), NOT mobile emulation
- Clipboard paste for email (Magritte React ignores fill())
- Char-by-char keyboard.press() for PIN code input
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import tempfile
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from .api_client import HHApiClient

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

HH_ANDROID_SCHEME = "hhandroid"

# Stealth script — hides Playwright/automation markers from HH
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
"""

# Selectors — applicant login form (role=applicant)
SEL_EMAIL_TAB = '[data-qa="credential-type-email"]'
SEL_EMAIL_TAB_LABEL = '[data-qa="credential-type-email"] + div, label:has([data-qa="credential-type-email"])'
SEL_PHONE_TAB = '[data-qa^="credential-type-phone"]'
SEL_LOGIN = 'input[data-qa="applicant-login-input-email"]'
SEL_PHONE_COUNTRY = 'input[data-qa="magritte-phone-input-calling-code-input"]'
SEL_PHONE_NUMBER = 'input[data-qa="magritte-phone-input-national-number-input"]'
SEL_SUBMIT = 'button[data-qa="submit-button"]'
SEL_EXPAND_PASSWORD = 'button[data-qa="expand-login-by-password"]'
SEL_PASSWORD = 'input[data-qa="login-input-password"]'
SEL_CODE_CONTAINER = 'div[data-qa="applicant-login-input-otp"]'
SEL_PIN = 'input[data-qa="magritte-pincode-input-field"]'
SEL_CAPTCHA = 'img[data-qa="account-captcha-picture"]'

SELECTOR_TIMEOUT = 30_000  # 30s (same as old bot)

# Paste shortcut
_PASTE_KEY = "Meta+v" if platform.system() == "Darwin" else "Control+v"

# Known country calling codes (order matters — longer prefixes first)
_COUNTRY_CODES = ["+998", "+375", "+7"]


def _parse_phone(username: str) -> tuple[str, str] | None:
    """
    Parse phone number into (country_code, national_number).
    Returns None if input doesn't look like a phone number.
    """
    # Remove all non-digit and non-plus chars except leading +
    digits = username.strip()
    digits = "".join(c for c in digits if c.isdigit() or c == "+")

    # Normalize Russian 8-prefix or 7-prefix to +7
    if digits.startswith("8") and len(digits) == 11:
        digits = "+7" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        digits = "+7" + digits[1:]
    elif len(digits) == 10 and not digits.startswith("+"):
        digits = "+7" + digits

    if not digits.startswith("+"):
        return None

    for code in _COUNTRY_CODES:
        if digits.startswith(code):
            national = digits[len(code):]
            return code, national

    return None


def _is_phone(username: str) -> bool:
    return _parse_phone(username) is not None


class HHAuth:
    """Headless OAuth authentication for HH.ru."""

    def __init__(self, client: HHApiClient, config: Config):
        self.client = client
        self.config = config
        self._screenshot_path: str | None = None

    async def start_login(self, username: str, progress=None) -> str:
        """
        Open headless browser, enter username, wait for OTP code page.
        Returns 'otp_pending'. Call submit_code() next.
        """
        self._username = username
        self._code_future: asyncio.Future[str | None] = asyncio.Future()
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._screenshot_path = None

        pw = await async_playwright().start()
        self._pw = pw

        try:
            launch_kwargs = dict(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                    "--lang=ru-RU",
                ],
            )

            self._browser = await pw.chromium.launch(**launch_kwargs)

            # Desktop Chrome context — NOT mobile (HH behaves differently on mobile)
            self._context = await self._browser.new_context(
                locale="ru-RU",
                timezone_id=os.environ.get("HH_BROWSER_TIMEZONE", "UTC"),
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                permissions=["clipboard-read", "clipboard-write"],
            )

            # Inject stealth script before any page loads
            await self._context.add_init_script(_STEALTH_JS)

            self._page = await self._context.new_page()

            # Intercept OAuth redirect (hhandroid:// scheme)
            def handle_request(request):
                url = request.url
                if url.startswith(f"{HH_ANDROID_SCHEME}://"):
                    logger.info("Intercepted OAuth redirect")
                    if not self._code_future.done():
                        sp = urlsplit(url)
                        code = parse_qs(sp.query).get("code", [None])[0]
                        if code:
                            self._code_future.set_result(code)

            self._page.on("request", handle_request)

            authorize_url = self.client.oauth.authorize_url
            logger.info("Navigating to OAuth: %s", authorize_url)
            await self._page.goto(authorize_url, timeout=60000, wait_until="domcontentloaded")

            # Switch to correct tab based on input type
            if _is_phone(username):
                try:
                    phone_label = self._page.get_by_text("Телефон", exact=True).first
                    await phone_label.click(timeout=5000)
                    await self._page.wait_for_timeout(500)
                    logger.info("Switched to phone tab")
                except Exception:
                    logger.debug("Phone tab switch skipped")

                parsed = _parse_phone(username)
                country_code, national_number = parsed

                # Fill country code
                await self._paste_into(self._page, SEL_PHONE_COUNTRY, country_code)
                await self._page.wait_for_timeout(300)

                # Wait for phone number input and fill
                try:
                    await self._page.wait_for_selector(SEL_PHONE_NUMBER, timeout=SELECTOR_TIMEOUT, state="visible")
                except Exception:
                    await self._save_debug_screenshot("phone_input_not_found")
                    raise RuntimeError(
                        f"Не найдено поле ввода телефона. Скриншот: {self._screenshot_path}"
                    )
                await self._paste_into(self._page, SEL_PHONE_NUMBER, national_number)
            else:
                try:
                    email_label = self._page.get_by_text("Почта", exact=True).first
                    await email_label.click(timeout=5000)
                    await self._page.wait_for_timeout(500)
                    logger.info("Switched to email tab")
                except Exception:
                    logger.debug("Email tab switch skipped (may already be on email form)")

                # Wait for email input
                try:
                    await self._page.wait_for_selector(SEL_LOGIN, timeout=SELECTOR_TIMEOUT, state="visible")
                except Exception:
                    await self._save_debug_screenshot("login_not_found")
                    raise RuntimeError(
                        f"Не найдено поле ввода email. Скриншот: {self._screenshot_path}"
                    )

                await self._paste_into(self._page, SEL_LOGIN, username)

            # Click submit button (Enter doesn't work reliably on Magritte forms)
            submit = await self._page.query_selector(SEL_SUBMIT)
            if submit:
                await submit.click()
            else:
                await self._page.press(SEL_LOGIN, "Enter")

            if progress:
                await progress("Отправляю код...")

            # Check for captcha
            if await self._check_captcha():
                await self._save_debug_screenshot("captcha")
                raise RuntimeError("CAPTCHA! Попробуйте позже или используйте другой IP.")

            # Wait for OTP code page
            try:
                await self._page.wait_for_selector(SEL_CODE_CONTAINER, timeout=SELECTOR_TIMEOUT)
            except Exception:
                await self._save_debug_screenshot("otp_not_found")
                raise RuntimeError(
                    f"Страница кода не загрузилась. Скриншот: {self._screenshot_path}"
                )

            logger.info("OTP code page loaded — waiting for user code")
            return "otp_pending"
        except Exception:
            await self._cleanup()
            raise

    async def submit_code(self, code: str) -> dict:
        """Submit OTP code and complete OAuth flow. Returns tokens + user info."""
        if not self._page:
            raise RuntimeError("Call start_login() first")

        try:
            # Wait for PIN input
            try:
                await self._page.wait_for_selector(SEL_PIN, timeout=10_000)
            except Exception:
                await self._save_debug_screenshot("pin_not_found")
                raise RuntimeError("Не найдено поле ввода кода")

            # Enter code char-by-char (more reliable for Magritte pincode component)
            await self._page.click(SEL_PIN)
            for char in code.strip():
                await self._page.keyboard.press(char)
                await asyncio.sleep(0.1)
            await asyncio.sleep(1)

            logger.debug("OTP code submitted, waiting for redirect...")

            # Wait for OAuth redirect
            try:
                auth_code = await asyncio.wait_for(self._code_future, timeout=30.0)
            except asyncio.TimeoutError:
                await self._save_debug_screenshot("redirect_timeout")
                raise RuntimeError(
                    f"OAuth redirect не получен. Скриншот: {self._screenshot_path}"
                )

            if not auth_code:
                raise RuntimeError("OAuth код не найден в redirect URL")

            # Exchange code for tokens
            tokens = self.client.oauth.exchange_code(auth_code)

            expires_at = time.time() + tokens.get("expires_in", 0)
            self.client.set_tokens(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                expires_at=expires_at,
            )

            # Get user info
            me = self.client.get_me()

            cookies = await self._context.cookies()

            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expires_at": expires_at,
                "hh_user_id": me.get("id"),
                "first_name": me.get("first_name"),
                "last_name": me.get("last_name"),
                "email": me.get("email"),
                "cookies_json": cookies,
            }
        finally:
            await self._cleanup()

    async def login_with_password(self, username: str, password: str) -> dict:
        """Full login with username + password (no OTP)."""
        self._code_future: asyncio.Future[str | None] = asyncio.Future()

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                locale="ru-RU",
                timezone_id=os.environ.get("HH_BROWSER_TIMEZONE", "UTC"),
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                permissions=["clipboard-read", "clipboard-write"],
            )
            await context.add_init_script(_STEALTH_JS)
            page = await context.new_page()

            def handle_request(request):
                url = request.url
                if url.startswith(f"{HH_ANDROID_SCHEME}://"):
                    if not self._code_future.done():
                        sp = urlsplit(url)
                        code = parse_qs(sp.query).get("code", [None])[0]
                        if code:
                            self._code_future.set_result(code)

            page.on("request", handle_request)

            await page.goto(
                self.client.oauth.authorize_url,
                timeout=60000,
                wait_until="domcontentloaded",
            )

            # Switch to email tab
            try:
                await page.get_by_text("Почта", exact=True).first.click(timeout=5000)
                await page.wait_for_timeout(500)
            except Exception:
                pass

            await page.wait_for_selector(SEL_LOGIN, timeout=SELECTOR_TIMEOUT)
            await self._paste_into(page, SEL_LOGIN, username)

            await page.click(SEL_EXPAND_PASSWORD)
            await page.wait_for_selector(SEL_PASSWORD, timeout=SELECTOR_TIMEOUT)
            await self._paste_into(page, SEL_PASSWORD, password)
            await page.press(SEL_PASSWORD, "Enter")

            auth_code = await asyncio.wait_for(self._code_future, timeout=30.0)
            if not auth_code:
                raise RuntimeError("Failed to get authorization code")

            tokens = self.client.oauth.exchange_code(auth_code)
            expires_at = time.time() + tokens.get("expires_in", 0)
            self.client.set_tokens(
                tokens["access_token"], tokens["refresh_token"], expires_at
            )
            me = self.client.get_me()

            cookies = await context.cookies()

            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expires_at": expires_at,
                "hh_user_id": me.get("id"),
                "first_name": me.get("first_name"),
                "last_name": me.get("last_name"),
                "email": me.get("email"),
                "cookies_json": cookies,
            }
        finally:
            await browser.close()
            await pw.stop()

    # ── Helpers ───────────────────────────────────────────────────

    async def _paste_into(self, page, selector: str, text: str) -> None:
        """Click input → clipboard paste. Works with Magritte React inputs."""
        el = page.locator(selector)
        await el.click()
        await page.wait_for_timeout(200)
        await page.evaluate("(text) => navigator.clipboard.writeText(text)", text)
        await page.keyboard.press(_PASTE_KEY)
        await page.wait_for_timeout(300)

    async def _check_captcha(self) -> bool:
        try:
            await self._page.wait_for_selector(SEL_CAPTCHA, timeout=3000, state="visible")
            return True
        except Exception:
            return False

    async def _save_debug_screenshot(self, label: str = "debug") -> str | None:
        if not self._page:
            return None
        try:
            path = os.path.join(tempfile.gettempdir(), f"hh_auth_{label}.png")
            await self._page.screenshot(path=path, full_page=True)
            self._screenshot_path = path
            logger.warning("Debug screenshot saved: %s", path)
            return path
        except Exception as exc:
            logger.warning("Failed to save screenshot: %s", exc)
            return None

    async def _cleanup(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
