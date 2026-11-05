"""Session keeper: owns one logged-in browser per portal account."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import socket
import time
import zlib

from ..config import Settings, read_secret_pair
from .browser import BrowserState
from .firewall import WriteFirewall
from .pdfcapture import PdfCapture
from .portals.base import Health, LoginError
from .runlog import RunLog
from .runner import RUN_LIMIT_S, RunResult, SkillRunner
from .skills import SkillLibrary


class SessionError(RuntimeError):
    pass


HANG = re.compile(r"Timeout|deadline reached|did not finish|no PDF was produced", re.I)


def _first_line(exc: Exception) -> str:
    line = (str(exc).splitlines() or [""])[0]
    return re.sub(r"\?\S*", "?…", line)[:160]


class _CountingOtp:

    def __init__(self, otp, keeper):
        self._otp, self._keeper = otp, keeper
        self._counted = set()

    async def begin(self):
        return await self._otp.begin()

    async def next_code(self, request, tried, timeout):
        if id(request) not in self._counted:
            self._counted.add(id(request))
            self._keeper.otps += 1
        return await self._otp.next_code(request, tried, timeout)


def without_identity(result: dict) -> dict:
    outputs = {k: v for k, v in (result.get("outputs") or {}).items() if k not in {"uhid", "patient_name"}}
    if isinstance(outputs.get("rows"), list):
        outputs["rows"] = [{**row, "cells": {k: v for k, v in (row.get("cells") or {}).items()
                                             if k.strip().lower() not in {"patient name", "registration no"}}}
                           if isinstance(row, dict) else row for row in outputs["rows"]]
    return {**result, "outputs": outputs}


CLOSE_LIMIT_S = 20
READ_SESSION_STORAGE_JS = "() => Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)]))"
WRITE_SESSION_STORAGE_JS = "(items) => { for (const [k, v] of Object.entries(items)) sessionStorage.setItem(k, v); }"


class SessionKeeper:
    def __init__(self, settings: Settings, portal, models, otp, *, headless: bool = False,
                 firewall_mode: str = "observe", profile_dir: Path | None = None,
                 library: SkillLibrary | None = None, credentials: tuple[str, str] | None = None,
                 setup=None, console: bool = True, login_timeout_s: float = 240,
                 health_timeout_s: float = 45, max_recoveries: int = 3, firewall_read_like: bool = False,
                 healthy_timeout_s: float = 480, keep_login: bool = False, timezone_id: str | None = None,
                 ledger_dir: Path | None = None, trust_login_s: float = 0, logout_on_stop: bool = True):
        self.settings = settings
        self.trust_login_s = trust_login_s
        self.logout_on_stop = logout_on_stop
        self._trusted_until = 0.0
        self.portal = portal
        self.models = models
        self.otp = otp
        self.headless = headless
        self.firewall_mode = firewall_mode
        self.firewall_read_like = firewall_read_like
        self.profile_dir = profile_dir or settings.profiles_dir / portal.name
        self.library = library or SkillLibrary()
        self._credentials = credentials
        self._setup = setup
        self.console = console
        self.login_timeout_s = login_timeout_s
        self.health_timeout_s = health_timeout_s
        self.max_recoveries = max_recoveries
        self.healthy_timeout_s = healthy_timeout_s
        self.keep_login = keep_login
        self.timezone_id = timezone_id
        self.ledger_dir = ledger_dir or settings.ledger_dir
        self.logins = self.otps = 0
        self._restored = None
        self._single_browser = None
        self._session_possible = False
        self._lock = asyncio.Lock()
        self._playwright = None
        self.context = None
        self.browser: BrowserState | None = None
        self.firewall: WriteFirewall | None = None
        self.capture: PdfCapture | None = None
        self.firewall_log: list[dict] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        if self.keep_login:
            self._claim_single_browser()
        self._playwright = await async_playwright().start()
        launch = dict(headless=self.headless, ignore_default_args=["--enable-automation"],
                      args=["--disable-popup-blocking"] + ([] if self.headless else ["--start-maximized"]))
        if self.settings.chrome_path:
            launch["executable_path"] = str(self.settings.chrome_path)
        else:
            launch["channel"] = "chrome"
        self._chrome = await self._playwright.chromium.launch(**launch)
        context_options = dict(accept_downloads=True, service_workers="block")
        if self.timezone_id:
            context_options["timezone_id"] = self.timezone_id
        if self.headless:
            context_options["viewport"] = {"width": 1366, "height": 900}
        else:
            context_options["no_viewport"] = True
        self._restored = self._read_saved_login() if self.keep_login else None
        if self._restored:
            context_options["storage_state"] = self._restored["state"]
            self.portal.dashboard_url = self._restored.get("dashboard_url")
            self._session_possible = True
        if os.environ.get("WARDOS_RECORD_DIR"):
            context_options["record_video_dir"] = os.environ["WARDOS_RECORD_DIR"]
            context_options["record_video_size"] = {"width": 1366, "height": 900}
        self.context = await self._chrome.new_context(**context_options)
        if self._setup:
            await self._setup(self.context)
        self.firewall = WriteFirewall(mode=self.firewall_mode, allow=self.portal.firewall_allow,
                                      hosts=self.portal.firewall_hosts, allow_read_like=self.firewall_read_like,
                                      on_event=lambda e: self.firewall_log.append(vars(e)))
        await self.firewall.attach(self.context)
        self.capture = PdfCapture(out_dir=self.settings.private_dir / "pdfs", origins=self.portal.origins)
        await self.capture.attach(self.context)
        self.browser = BrowserState(self.context, self.portal.origins)

    async def stop(self) -> None:
        if self.keep_login and self.context is not None:
            try:
                await asyncio.wait_for(self.save_login(), timeout=CLOSE_LIMIT_S)
            except asyncio.TimeoutError:
                pass
        elif self.logout_on_stop and self.browser is not None and self.context is not None:
            try:
                page = await self.browser.active()
                if any(page.url.startswith(o) for o in self.portal.origins) and not await self.portal.looks_logged_out(page):
                    log = RunLog.create(self.settings.runs_dir, "logout", console=False)
                    start = len(self.firewall_log)
                    try:
                        await asyncio.wait_for(self.portal.logout(self.browser, log=log), timeout=20)
                    finally:
                        log.write_json("firewall.json", self.firewall_log[start:])
            except Exception:
                pass
        for closer in (self.context, getattr(self, "_chrome", None)):
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=CLOSE_LIMIT_S)
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=CLOSE_LIMIT_S)
            except Exception:
                pass
        self.context = self._chrome = self._playwright = None
        if self._single_browser is not None:
            self._single_browser.close()
            self._single_browser = None

    @property
    def login_file(self) -> Path:
        return self.settings.private_dir / "sessions" / f"{self.portal.name}.json"

    def _claim_single_browser(self) -> None:
        claim = socket.socket()
        try:
            claim.bind(("127.0.0.1", 47000 + zlib.crc32(self.portal.home_url.encode()) % 1000))
        except OSError:
            claim.close()
            raise SessionError("another WardOS job is using this hospital login; wait for it or stop it first") from None
        self._single_browser = claim

    def _read_saved_login(self) -> dict | None:
        try:
            saved = json.loads(self.login_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return saved if isinstance(saved, dict) and saved.get("state") else None

    async def save_login(self) -> None:
        if not self._session_possible or self.browser is None:
            return
        try:
            page = await self.browser.active()
            if not any(page.url.startswith(o) for o in self.portal.origins) or await self.portal.looks_logged_out(page):
                return
            saved = {"state": await self.context.storage_state(),
                     "session_storage": await page.evaluate(READ_SESSION_STORAGE_JS),
                     "dashboard_url": self.portal.dashboard_url}
        except Exception:
            return
        self.login_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.login_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved), encoding="utf-8")
        temporary.replace(self.login_file)

    def forget_login(self) -> None:
        self.login_file.unlink(missing_ok=True)

    async def _open_restored(self, log: RunLog) -> None:
        saved, self._restored = self._restored, None
        page = await self.browser.active()
        inside = saved.get("dashboard_url") or self.portal.home_url
        await page.goto(inside, wait_until="domcontentloaded", timeout=45000)
        if saved.get("session_storage"):
            await page.evaluate(WRITE_SESSION_STORAGE_JS, saved["session_storage"])
            await page.goto(inside, wait_until="domcontentloaded", timeout=45000)
        log.event("saved_login_tried")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()

    def _runner(self, log: RunLog) -> SkillRunner:
        return SkillRunner(self.browser, portal=self.portal, capture=self.capture, library=self.library,
                           log=log, ledger_dir=self.ledger_dir)

    async def health(self, log: RunLog) -> Health:
        if not self._session_possible:
            log.event("health", state=Health.LOGGED_OUT.value, fresh_browser=True)
            return Health.LOGGED_OUT
        if time.monotonic() < self._trusted_until:
            log.event("health", state=Health.HEALTHY.value, just_logged_in=True)
            return Health.HEALTHY
        if self._restored:
            await self._open_restored(log)
        page = await self.browser.active()
        if not any(page.url.startswith(origin) for origin in self.portal.origins):
            await page.goto(self.portal.home_url, wait_until="domcontentloaded", timeout=45000)
        if await self.portal.looks_logged_out(page):
            state = Health.LOGGED_OUT
        else:
            result = await self._runner(log).run(self.portal.health_skill, {}, deadline_s=self.health_timeout_s)
            if result.status in {"OK", "FOUND"}:
                state = Health.HEALTHY
            elif await self.portal.looks_logged_out(await self.browser.active()):
                state = Health.LOGGED_OUT
            else:
                state = Health.BROKEN
        log.event("health", state=state.value)
        return state

    async def ensure_healthy(self, log: RunLog) -> int:
        recoveries = 0
        looked_again = False
        give_up = time.monotonic() + self.healthy_timeout_s
        checks_left = self.max_recoveries + 1
        while checks_left > 0:
            if time.monotonic() >= give_up:
                break
            checks_left -= 1
            try:
                state = await self.health(log)
            except Exception as exc:
                log.event("health_error", reason=type(exc).__name__, detail=_first_line(exc))
                log.say(f"The portal did not load ({type(exc).__name__}); trying again")
                await asyncio.sleep(3)
                state = Health.LOGGED_OUT
            if state is Health.HEALTHY:
                log.say("Hospital session is healthy")
                if self.keep_login and recoveries:
                    await self.save_login()
                return recoveries
            if state is Health.BROKEN and self.keep_login and not looked_again and not self.logins:
                looked_again = True
                checks_left += 1
                log.event("saved_login_second_look")
                continue
            if state is Health.BROKEN:
                log.say("Session looks logged in but the module is missing or stuck: logging out")
                await self.logout(log)
            recoveries += 1
            await self._login(log, deadline=min(give_up, time.monotonic() + self.login_timeout_s))
        raise SessionError(f"no healthy session after {recoveries} login attempt(s)"
                           f" (limit {self.max_recoveries} recoveries / {self.healthy_timeout_s / 60:.0f} min)")

    async def logout(self, log: RunLog) -> None:
        await self.portal.logout(self.browser, log=log)
        self._session_possible = False
        self._trusted_until = 0.0
        if self.keep_login:
            self.forget_login()

    async def _login(self, log: RunLog, deadline: float | None = None) -> None:
        username, password = self._credentials or read_secret_pair(
            self.settings.portal_secrets_file, *self.portal.credential_keys)
        started = time.monotonic()
        log.say("Logging in to the hospital portal")
        self._session_possible = True
        self.logins += 1
        try:
            await self.portal.login(self.browser, username=username, password=password, models=self.models,
                                    otp=_CountingOtp(self.otp, self), log=log, deadline=deadline or started + self.login_timeout_s)
        except LoginError as exc:
            log.event("login_failed", reason=str(exc), retryable=exc.retryable)
            if not exc.retryable:
                raise SessionError(str(exc)) from exc
            log.say(f"Login attempt failed: {exc}")
            return
        except Exception as exc:
            log.event("login_failed", reason=type(exc).__name__, detail=_first_line(exc), retryable=True)
            log.say(f"Login attempt failed ({type(exc).__name__}); trying again")
            return
        self._trusted_until = time.monotonic() + self.trust_login_s
        log.event("login_done", seconds=round(time.monotonic() - started, 2))
        log.say(f"Login finished in {time.monotonic() - started:.1f}s")

    async def run_skill(self, name: str, inputs: dict | None = None, *, log: RunLog | None = None,
                        deadline_s: float = RUN_LIMIT_S) -> RunResult:
        async with self._lock:
            log = log or RunLog.create(self.settings.runs_dir, name.replace(".", "-"), console=self.console)
            recoveries = await self.ensure_healthy(log)
            skill = self.library.load(self.portal.name, name)
            result = await self._runner(log).run(name, inputs, deadline_s=deadline_s)
            stuck = bool(HANG.search(result.reason or ""))
            if result.status == "STEP_FAILED" and (result.session_suspect or stuck) and skill["kind"] == "read":
                log.event("session_retry", reason="session" if result.session_suspect else "hang")
                log.say(("Session broke" if result.session_suspect else "The page hung")
                        + " during the task: logging out, logging in, running it again")
                await self.logout(log)
                recoveries += 1 + await self.ensure_healthy(log)
                result = await self._runner(log).run(name, inputs, deadline_s=deadline_s)
            result.session_recoveries = recoveries
            log.write_json("result.json", without_identity(result.to_dict()))
            if self.firewall_log:
                log.write_json("firewall.json", self.firewall_log)
            return result

    async def keep_alive(self, interval_s: float, stop: asyncio.Event, log: RunLog) -> None:
        while not stop.is_set():
            async with self._lock:
                await self.ensure_healthy(log)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
