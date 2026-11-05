"""Shared browser state: the context, the active portal tab, and a network-quiet tracker."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit


BUSY_JS = (
    "() => { const v = e => !!(e.offsetWidth || e.offsetHeight); "
    "return Array.from(document.querySelectorAll('.ngx-spinner-overlay, .swal2-loading, .loader, .spinner'))"
    ".some(v) || /please\\s+wait/i.test((document.querySelector('.ngx-spinner-overlay, .overlay, "
    ".loading-text') || {}).innerText || ''); }")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


class BrowserState:
    def __init__(self, context, origins: list[str]):
        self.context = context
        self.origins = origins
        self.page = None
        self._inflight: set[int] = set()
        self._last_change = time.monotonic()
        context.on("request", self._on_request)
        context.on("requestfinished", self._on_done)
        context.on("requestfailed", self._on_done)

    def _on_request(self, request) -> None:
        if request.resource_type in ("xhr", "fetch"):
            self._inflight.add(id(request))
            self._last_change = time.monotonic()

    def _on_done(self, request) -> None:
        if id(request) in self._inflight:
            self._inflight.discard(id(request))
            self._last_change = time.monotonic()

    async def network_quiet(self, quiet_ms: int = 600, timeout_s: float = 30) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._inflight and (time.monotonic() - self._last_change) * 1000 >= quiet_ms:
                return True
            await asyncio.sleep(0.1)
        return False

    async def settle(self, timeout_s: float = 25) -> None:
        await self.network_quiet(500, 2.5)
        page = await self.active()
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                busy = await page.evaluate(BUSY_JS)
            except Exception:
                busy = False
            if not busy:
                return
            await asyncio.sleep(0.3)

    def portal_pages(self) -> list:
        return [p for p in self.context.pages
                if not p.is_closed() and origin_of(p.url) in self.origins]

    async def active(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        pages = self.portal_pages()
        if pages:
            self.page = pages[-1]
        elif self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()
        return self.page

    async def close_extra_tabs(self) -> None:
        keep = await self.active()
        for page in list(self.context.pages):
            if page is keep or page.is_closed():
                continue
            if page.url.startswith("blob:") or page.url in ("", "about:blank"):
                try:
                    await page.close()
                except Exception:
                    pass
