"""OpenMRS 3 ("O3") profile - the second, unrelated hospital system used to prove the explorer is not eHospital-specific."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
import re
import time

from ...config import openmrs_host, openmrs_login, openmrs_url
from .base import LoginError

USERNAME = "input#username, input[name='username']"
PASSWORD = "input#password, input[name='password'], input[type='password']"
SUBMIT = "button[type='submit']"
LOGIN_ERROR = ".cds--inline-notification--error"


@dataclass
class OpenMRSPortal:
    name: str = "openmrs"
    base: str = field(default_factory=openmrs_url)
    origins: list[str] = field(default_factory=lambda: [openmrs_url()])
    firewall_hosts: list[str] = field(default_factory=lambda: [openmrs_host()])
    firewall_allow: list[str] = field(default_factory=lambda: [r"^/openmrs/ws/rest/v1/session"])
    credential_keys: tuple[str, str] = ("OPENMRS_USERNAME", "OPENMRS_PASSWORD")

    @property
    def demo_credentials(self) -> tuple[str, str]:
        return openmrs_login()
    health_skill: str = "nav.home"
    location: str = field(default_factory=lambda: os.environ.get("OPENMRS_LOCATION") or "Ward 2")
    dashboard_url: str | None = None
    fresh_home: bool = False

    @property
    def home_url(self) -> str:
        return f"{self.base}/openmrs/spa/home"

    @property
    def login_url(self) -> str:
        return f"{self.base}/openmrs/spa/login"

    async def login(self, browser, *, username, password, models, otp, log, deadline: float) -> None:
        page = await browser.active()
        await page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
        await page.locator(USERNAME).first.wait_for(state="visible", timeout=30000)
        await page.locator(USERNAME).first.fill(username)
        if not await page.locator(PASSWORD).first.is_visible():
            await page.locator(SUBMIT).first.click()
        await page.locator(PASSWORD).first.wait_for(state="visible", timeout=15000)
        await page.locator(PASSWORD).first.fill(password)
        await page.locator(SUBMIT).first.click()
        end = min(deadline, time.monotonic() + 45)
        while time.monotonic() < end:
            if "/home" in page.url:
                break
            radios = page.get_by_role("radio")
            if await radios.count():
                wanted = page.get_by_role("radio", name=self.location, exact=True)
                await (wanted if await wanted.count() else radios).first.check(force=True)
                await page.locator(SUBMIT).first.click()
                log.event("login_location_chosen")
            elif "/login" in page.url and await page.locator(LOGIN_ERROR).count():
                raise LoginError("portal rejected the username/password", retryable=False)
            await asyncio.sleep(0.5)
        if "/home" not in page.url:
            raise LoginError("did not reach the home page after login")
        await self._pin_english(page, log=log)
        await self._use_location(page, log=log)
        self.dashboard_url = page.url
        self.fresh_home = True
        log.event("login_home_visible")

    async def _pin_english(self, page, *, log) -> None:
        try:
            done = await page.evaluate("""async () => {
                const url = '/openmrs/ws/rest/v1/session';
                const now = await (await fetch(url, {credentials: 'same-origin'})).json();
                const locale = (now.locale || '').toLowerCase();
                if (locale.startsWith('en')) return false;
                const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({locale: 'en'})});
                return r.ok;
            }""")
            if done:
                await page.reload(wait_until="domcontentloaded")
                log.event("session_language_set_english")
        except Exception:
            log.event("session_language_check_skipped")

    async def _use_location(self, page, *, log) -> None:
        header = page.locator("header")
        if await header.get_by_text(self.location, exact=True).count():
            return
        await header.locator("button[aria-label*='ocation' i], [data-testid*='location' i]").first.click()
        await page.get_by_role("radio", name=self.location, exact=True).check(force=True)
        remember = page.get_by_role("checkbox", name=re.compile("remember", re.I))
        if await remember.count() and await remember.first.is_checked():
            await remember.first.uncheck(force=True)
        await page.locator(SUBMIT).first.click()
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if "/login" not in page.url and await header.get_by_text(self.location, exact=True).count():
                log.event("location_set", location=self.location)
                return
            await asyncio.sleep(0.5)
        raise LoginError(f"could not switch to {self.location}")

    async def logout(self, browser, *, log) -> None:
        await browser.context.clear_cookies()
        for page in browser.portal_pages():
            try:
                await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
            except Exception:
                pass
        page = await browser.active()
        await page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
        log.event("session_cleared")

    async def looks_logged_out(self, page) -> bool:
        try:
            return "/login" in page.url or await page.locator(USERNAME).first.is_visible()
        except Exception:
            return False

    async def session_suspect(self, page) -> bool:
        return await self.looks_logged_out(page)

    async def go_home(self, browser, *, log):
        page = await browser.active()
        for other in list(browser.context.pages):
            if other is not page and not other.is_closed():
                try:
                    await other.close()
                except Exception:
                    pass
        fresh, self.fresh_home = self.fresh_home, False
        if not (fresh and "/home" in page.url):
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=60000)
        end = time.monotonic() + 20
        while time.monotonic() < end and "/home" not in page.url:
            await asyncio.sleep(0.3)
        if "/home" not in page.url:
            raise LoginError("could not get back to the home page")
        browser.page = page
        self.dashboard_url = page.url
        log.event("dashboard")
        return page
