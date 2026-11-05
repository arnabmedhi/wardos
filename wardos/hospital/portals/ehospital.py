"""NIC NextGen eHospital profile."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import re
import time

from ..targets import TargetNotFound, is_visible, resolve
from .base import LoginError

USERNAME = 'input[formcontrolname="username"]'
PASSWORD = 'input[formcontrolname="password"], input[type="password"]'
CAPTCHA_IMG = "img.img-control"
CAPTCHA_INPUT = 'input[formcontrolname="captchaValue"]'
CAPTCHA_REFRESH = 'i.fa-refresh, i.fa-sync, i.fa-redo, [title*="refresh" i], [aria-label*="refresh" i]'
OTP_INPUT = ('input[formcontrolname*="otp" i], input[name*="otp" i], input[id*="otp" i], '
             'input[placeholder*="otp" i], input[autocomplete="one-time-code"]')
SWAL_YES = "button.swal2-confirm"
LOGIN_LINK = {"role": "link", "name": r"/^\s*log\s*-?\s*in\s*$/i", "text": r"/^\s*log\s*-?\s*in\s*$/i"}
SUBMIT = {"role": "button", "name": r"/^\s*(send\s*otp|log\s*-?\s*in)\s*$/i"}
OTP_SUBMIT = {"role": "button", "name": r"/^\s*(verify|validate|submit|confirm)(\s*otp)?\s*$|^\s*log\s*-?\s*in\s*$/i"}
SKIP = {"role": "button", "name": r"/^\s*skip\s*$/i", "text": r"/^\s*skip\s*$/i"}
LOGOUT = {"role": "link", "name": r"/^\s*(log|sign)\s*-?\s*out\s*$/i", "text": r"/^\s*(log|sign)\s*-?\s*out\s*$/i"}
USER_MENU = "a.header_circle:has(em.fa-user)"
MENU_LOGOUT = ".cdk-overlay-container a.list-group-item"
HOME_MARKER = {"text": "/Lab Information System/i"}

OUTCOME_JS = r"""
(oldSrc) => {
  const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                    && getComputedStyle(el).visibility !== 'hidden';
  const q = s => Array.from(document.querySelectorAll(s)).filter(vis);
  if (q('%OTP%').length) return 'OTP';
  for (const s of q('.swal2-popup')) {
    const t = s.innerText || '';
    if (/already/i.test(t) && /log/i.test(t)) return 'ALREADY';
  }
  // Only messages that appeared after this submit: older ones were tagged before it (a leftover
  // "invalid captcha" toast made correct reads look rejected, live 23 Oct), and fading pop-ups don't count.
  const alerts = q('.alert-danger, .swal2-html-container, .swal2-title, .invalid-feedback, mat-error, .toast-message, .toast-error, .text-danger')
                   .filter(e => !e.closest('[data-wardos-old]') && !e.closest('.swal2-hide'))
                   .map(e => e.innerText || '').join(' ');
  if (/captcha/i.test(alerts) && /invalid|incorrect|wrong|mismatch/i.test(alerts)) return 'CAPTCHA_BAD';
  if (/invalid|incorrect|wrong/i.test(alerts) && /user|password|credential/i.test(alerts)
      && !/otp|one.?time/i.test(alerts)) return 'CRED_BAD';
  if (!q('input[type=password]').length && !q('input[formcontrolname="username"]').length) {
    const tile = q('a, button, div, span').some(e => /^\s*Lab Information System\s*$/i.test(e.innerText || ''));
    return tile ? 'HOME' : 'LEFT_LOGIN';
  }
  const img = document.querySelector('img.img-control');
  const field = document.querySelector('input[formcontrolname="captchaValue"]');
  if (img && oldSrc && img.getAttribute('src') !== oldSrc && field && !field.value) return 'CAPTCHA_RESET';
  return '';
}
""".replace("%OTP%", OTP_INPUT.replace("'", "\\'"))

OLD_ALERTS_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  let closed = 0;
  for (const p of document.querySelectorAll('.swal2-popup')) {          // a leftover error pop-up: close it
    if (!vis(p) || /already/i.test(p.innerText || '')) continue;
    const ok = p.querySelector('.swal2-confirm, .swal2-close');
    if (ok) { ok.click(); closed++; }
  }
  for (const e of document.querySelectorAll('.alert-danger, .swal2-popup, .invalid-feedback, mat-error, .toast-message, '
                                          + '.toast-error, .text-danger, .toast-container > *, #toast-container > *'))
    e.setAttribute('data-wardos-old', '1');
  return closed;
}
"""

NOTICES_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const seen = new Set();
  for (const e of document.querySelectorAll('.swal2-popup, [class*=toast], mat-snack-bar-container, [role=alert], '
                                          + '.alert, .alert-danger, mat-error, .invalid-feedback, .text-danger, [class*=error]')) {
    if (!vis(e)) continue;
    const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 160 && !/helpdesk/i.test(t)) seen.add(t);
  }
  return Array.from(seen).slice(0, 4).join(' | ');
}
"""

VISIBLE_ERRORS_JS = r"""
() => Array.from(document.querySelectorAll('.alert-danger, .swal2-html-container, .toast-message, mat-error, .text-danger, .invalid-feedback'))
  .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden')
  .map(e => e.innerText || '').join(' ')
"""

SUSPECT_TEXT = re.compile(r"session\s+(has\s+)?expired|unauthori[sz]ed|please\s+log\s*in\s+again|token\s+expired", re.I)


@dataclass
class EHospitalPortal:
    name: str = "ehospital"
    home_url: str = "https://nextgen.ehospital.gov.in/"
    origins: list[str] = field(default_factory=lambda: ["https://nextgen.ehospital.gov.in"])
    firewall_hosts: list[str] = field(default_factory=lambda: ["nextgen.ehospital.gov.in"])
    firewall_allow: list[str] = field(default_factory=lambda: [r"^/api/authentication/", r"^/api/user_mgmt/v1/logout$"])
    credential_keys: tuple[str, str] = ("LIS_USERNAME", "LIS_PASSWORD")
    health_skill: str = "nav.lis_report"
    max_captcha_attempts: int = 8
    dashboard_url: str | None = None

    async def login(self, browser, *, username, password, models, otp, log, deadline: float) -> None:
        page = await browser.active()
        await self._open_login_form(page, log, deadline, fresh=True)
        tried_codes: set[str] = set()
        model_failures = rejections = 0
        for attempt in range(1, self.max_captcha_attempts + 1):
            self._check(deadline, "login")
            await self._fill_if_empty(page, USERNAME, username)
            await self._fill_if_empty(page, PASSWORD, password)
            image, mime, src = await self._captcha_image(page)
            length = await self._captcha_length(page)
            try:
                text = await models.read_captcha(image, mime, length)
                model_failures = 0
                log.event("captcha_read", attempt=attempt, usable=bool(text))
            except Exception as exc:
                model_failures += 1
                log.event("captcha_read", attempt=attempt, usable=False, error=type(exc).__name__)
                if model_failures >= 3:
                    raise LoginError(f"CAPTCHA reader failed {model_failures} times in a row ({type(exc).__name__})")
                text = ""
            if not text:
                await self._refresh_captcha(page, src)
                continue
            if await page.locator(CAPTCHA_IMG).first.get_attribute("src") != src:
                continue
            await page.locator(CAPTCHA_INPUT).first.fill(text)
            if await page.evaluate(OLD_ALERTS_JS):
                await asyncio.sleep(0.4)
                if (await page.locator(CAPTCHA_IMG).first.get_attribute("src") != src
                        or await page.locator(CAPTCHA_INPUT).first.input_value() != text):
                    continue
            request = await otp.begin()
            submit = await resolve(page, SUBMIT, 10000)
            await submit.locator.click()
            outcome = await self._wait_outcome(page, src, min(30.0, self._left(deadline)))
            try:
                message = (await page.evaluate(NOTICES_JS))[:160]
            except Exception:
                message = ""
            log.event("login_outcome", attempt=attempt, outcome=outcome or "NO_RESPONSE", message=message)
            self._keep_captcha_sample(log, attempt, image, mime, text, outcome)
            if outcome == "CAPTCHA_RESET":
                outcome = "CAPTCHA_BAD"
            if outcome == "OTP":
                log.say("Portal asked for an OTP; reading the inbox")
                await self._submit_otp(page, otp, request, tried_codes, log, deadline)
                break
            if outcome in ("HOME", "LEFT_LOGIN"):
                break
            if outcome == "ALREADY":
                log.say("Portal says this account is already logged in; confirming a fresh login")
                await page.locator(SWAL_YES).first.click()
                await asyncio.sleep(0.6)
                if await page.locator(CAPTCHA_IMG).first.get_attribute("src") == src:
                    await self._refresh_captcha(page, src)
                continue
            if outcome == "CAPTCHA_BAD":
                rejections += 1
                log.say(f"CAPTCHA not accepted (attempt {attempt}); trying a new one")
                if rejections >= 3:
                    log.event("login_form_reloaded", after=rejections)
                    rejections = 0
                    await self._open_login_form(page, log, deadline, fresh=True)
                elif await page.locator(CAPTCHA_IMG).first.get_attribute("src") == src:
                    await self._refresh_captcha(page, src)
                continue
            if outcome == "CRED_BAD":
                raise LoginError("portal rejected the username/password", retryable=False)
            raise LoginError("no response from the portal after submitting the login form")
        else:
            raise LoginError(f"CAPTCHA not accepted after {self.max_captcha_attempts} attempts")
        await self._after_login(browser, log, deadline)

    async def _open_login_form(self, page, log, deadline, fresh: bool = False):
        if fresh or not page.url.startswith(self.home_url.rstrip("/")):
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        if await page.locator(USERNAME).first.is_visible():
            return
        try:
            link = await resolve(page, LOGIN_LINK, 15000)
            await link.locator.click()
        except TargetNotFound:
            await page.goto(self.home_url.rstrip("/") + "/login", wait_until="domcontentloaded", timeout=45000)
        await page.locator(USERNAME).first.wait_for(state="visible", timeout=int(min(25, self._left(deadline)) * 1000))
        log.event("login_form_open")

    async def _fill_if_empty(self, page, selector: str, value: str):
        field_ = page.locator(selector).first
        if not await field_.input_value():
            await field_.fill(value)

    async def _captcha_image(self, page) -> tuple[bytes, str, str]:
        img = page.locator(CAPTCHA_IMG).first
        await img.wait_for(state="visible", timeout=20000)
        src = await img.get_attribute("src") or ""
        match = re.fullmatch(r"data:(image/[a-z+]+);base64,(.+)", src.strip(), re.S)
        if match:
            return base64.b64decode(re.sub(r"\s+", "", match.group(2))), match.group(1), src
        return await img.screenshot(timeout=10000), "image/png", src

    @staticmethod
    def _keep_captcha_sample(log, attempt, image: bytes, mime: str, read: str, outcome: str):
        folder = getattr(log, "directory", None)
        if folder is None:
            return
        try:
            target = folder / "captcha"
            target.mkdir(exist_ok=True)
            suffix = "png" if "png" in mime else "jpg"
            number = len(list(target.glob("*.png")) + list(target.glob("*.jpg"))) + 1
            (target / f"{number:02d}-a{attempt}-{outcome or 'NO_RESPONSE'}.{suffix}").write_bytes(image)
            with open(target / "reads.txt", "a", encoding="utf-8") as handle:
                handle.write(f"{number}\t{attempt}\t{read}\t{outcome or 'NO_RESPONSE'}\n")
        except OSError:
            pass

    async def _captcha_length(self, page) -> int | None:
        value = await page.locator(CAPTCHA_INPUT).first.get_attribute("maxlength")
        return int(value) if value and value.isdigit() else None

    async def _refresh_captcha(self, page, old_src: str):
        for _ in range(3):
            control = page.locator(CAPTCHA_REFRESH).first
            if await control.count():
                await control.click(force=True)
            else:
                await page.reload(wait_until="domcontentloaded")
                return
            end = time.monotonic() + 8
            while time.monotonic() < end:
                if (await page.locator(CAPTCHA_IMG).first.get_attribute("src") or "") != old_src:
                    return
                await asyncio.sleep(0.2)
        raise LoginError("CAPTCHA image did not refresh")

    async def _wait_outcome(self, page, old_src: str, timeout: float) -> str:
        end = time.monotonic() + timeout
        swapped_at = None
        while time.monotonic() < end:
            try:
                outcome = await page.evaluate(OUTCOME_JS, old_src)
            except Exception:
                outcome = ""
            if outcome == "CAPTCHA_RESET":
                swapped_at = swapped_at or time.monotonic()
                if time.monotonic() - swapped_at < 5:
                    await asyncio.sleep(0.25)
                    continue
                return outcome
            if outcome:
                return outcome
            await asyncio.sleep(0.25)
        return "CAPTCHA_RESET" if swapped_at else ""

    async def _submit_otp(self, page, otp, request, tried: set[str], log, deadline):
        while self._left(deadline) > 0:
            code = await otp.next_code(request, tried, timeout=min(90.0, self._left(deadline)))
            if not code:
                break
            tried.add(code)
            boxes = page.locator(OTP_INPUT)
            count = await boxes.count()
            if count >= 4 and (await boxes.first.get_attribute("maxlength")) == "1":
                for index, digit in enumerate(code[:count]):
                    await boxes.nth(index).fill(digit)
            else:
                await boxes.first.fill(code)
            button = await resolve(page, OTP_SUBMIT, 10000)
            await button.locator.click()
            log.event("otp_submitted", attempt=len(tried))
            end = time.monotonic() + 20
            rejected = False
            while time.monotonic() < end:
                try:
                    if not await page.locator(OTP_INPUT).first.is_visible():
                        log.event("otp_accepted")
                        return
                    text = await page.evaluate(VISIBLE_ERRORS_JS)
                except Exception:
                    await asyncio.sleep(0.3)
                    continue
                if re.search(r"otp|one.?time", text, re.I) and re.search(r"invalid|incorrect|expired|wrong", text, re.I):
                    rejected = True
                    log.event("otp_rejected", attempt=len(tried))
                    break
                await asyncio.sleep(0.3)
            if not rejected:
                raise LoginError("OTP submitted but the portal did not respond")
        raise LoginError("no valid OTP arrived in time")

    async def _after_login(self, browser, log, deadline):
        page = await browser.active()
        end = time.monotonic() + min(20.0, self._left(deadline))
        while time.monotonic() < end:
            if await is_visible(page, SKIP):
                await (await resolve(page, SKIP, 3000)).locator.click()
                log.event("hpr_notice_skipped")
                await asyncio.sleep(0.5)
                continue
            if await is_visible(page, HOME_MARKER):
                self.dashboard_url = page.url
                log.event("login_home_visible")
                return
            await asyncio.sleep(0.3)
        log.event("login_home_not_confirmed")

    async def _click_logout(self, page) -> str | None:
        if await is_visible(page, LOGOUT):
            await (await resolve(page, LOGOUT, 3000)).locator.click()
            return "link"
        icon = page.locator(USER_MENU).first
        if await icon.count() and await icon.is_visible():
            await icon.click(timeout=5000)
            item = page.locator(MENU_LOGOUT, has_text=re.compile(r"^\s*(log|sign)\s*-?\s*out\s*$", re.I)).first
            await item.wait_for(state="visible", timeout=5000)
            await item.click(timeout=5000)
            return "profile_menu"
        return None

    async def logout(self, browser, *, log) -> None:
        for page in browser.portal_pages():
            try:
                how = await self._click_logout(page)
                if how:
                    end = time.monotonic() + 8
                    while time.monotonic() < end and not await self.looks_logged_out(page):
                        await asyncio.sleep(0.3)
                    log.event("logout_clicked", via=how)
                    break
            except Exception:
                continue
        await browser.context.clear_cookies()
        for page in browser.portal_pages():
            try:
                await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
            except Exception:
                pass
        pages = list(browser.context.pages)
        keep = pages[0] if pages else await browser.context.new_page()
        for page in pages[1:]:
            try:
                await page.close()
            except Exception:
                pass
        browser.page = keep
        await keep.goto(self.home_url, wait_until="domcontentloaded", timeout=45000)
        log.event("session_cleared")

    async def _on_dashboard(self, page) -> bool:
        return "/lis" not in page.url and await is_visible(page, HOME_MARKER)

    async def go_home(self, browser, *, log) -> object:
        for page in browser.portal_pages():
            if await self._on_dashboard(page):
                break
        else:
            page = await browser.active()
            for url in [u for u in (self.dashboard_url, self.home_url) if u]:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                end = time.monotonic() + 8
                while time.monotonic() < end and not await self._on_dashboard(page):
                    await asyncio.sleep(0.3)
                if await self._on_dashboard(page):
                    break
            else:
                raise LoginError("could not get back to the dashboard")
        for other in list(browser.context.pages):
            if other is not page and not other.is_closed():
                try:
                    await other.close()
                except Exception:
                    pass
        browser.page = page
        self.dashboard_url = page.url
        end = time.monotonic() + 4
        while time.monotonic() < end:
            if await is_visible(page, SKIP):
                try:
                    await (await resolve(page, SKIP, 2000)).locator.click()
                    log.event("hpr_notice_skipped")
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                break
            await asyncio.sleep(0.3)
        log.event("dashboard")
        return page

    async def looks_logged_out(self, page) -> bool:
        try:
            if await page.locator(PASSWORD).first.is_visible() or await page.locator(USERNAME).first.is_visible():
                return True
            return await is_visible(page, LOGIN_LINK) and not await is_visible(page, HOME_MARKER)
        except Exception:
            return False

    async def session_suspect(self, page) -> bool:
        if await self.looks_logged_out(page):
            return True
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        if SUSPECT_TEXT.search(body):
            return True
        if "/lis" in page.url and not re.search(r"LIS\s+Transaction|Report", body, re.I):
            return True
        return False

    @staticmethod
    def _left(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _check(self, deadline: float, what: str):
        if self._left(deadline) <= 0:
            raise LoginError(f"{what}: time limit reached")
