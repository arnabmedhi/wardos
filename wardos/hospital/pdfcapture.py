"""Keep the hospital's own PDF, whichever way the portal delivers it."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

MAX_PDF_BYTES = 24 * 1024 * 1024

HOOK_SCRIPT = """
(origins) => {
  if (!origins.includes(location.origin) || window.__wardosPdfHook) return;
  window.__wardosPdfHook = true;
  const original = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function (obj) {
    const url = original(obj);
    try {
      if (obj && typeof obj.slice === 'function' && obj.size <= %MAX%) {
        obj.slice(0, 5).text().then(prefix => {
          if (prefix !== '%PDF-') return;
          return obj.arrayBuffer().then(buffer => {
            const bytes = new Uint8Array(buffer); let bin = '';
            for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
            return window.__wardosKeepPdf(btoa(bin));
          });
        }).catch(() => {});
      }
    } catch (e) {}
    return url;
  };
}
""".replace("%MAX%", str(MAX_PDF_BYTES))

FETCH_BLOB = """async () => {
  const r = await fetch(location.href); const buf = await r.arrayBuffer();
  const bytes = new Uint8Array(buf); let bin = '';
  for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(bin);
}"""


@dataclass
class CapturedPdf:
    path: Path
    sha256: str
    via: str
    size: int


@dataclass
class PdfCapture:
    out_dir: Path
    origins: list[str]
    captured: list[CapturedPdf] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set, repr=False)
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _tasks: set = field(default_factory=set, repr=False)

    def start_run(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.captured = []
        self._seen = set()

    def _keep(self, data: bytes, via: str) -> None:
        if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._seen:
            return
        self._seen.add(digest)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{digest[:16]}.pdf"
        path.write_bytes(data)
        self.captured.append(CapturedPdf(path, digest, via, len(data)))
        self._event.set()

    async def attach(self, context) -> None:
        async def keep_from_hook(source, encoded):
            frame_url = source["frame"].url if source.get("frame") else ""
            if self._origin(frame_url) not in self.origins:
                return
            if isinstance(encoded, str) and len(encoded) <= MAX_PDF_BYTES * 2:
                self._keep(base64.b64decode(encoded, validate=True), "blob-hook")

        await context.expose_binding("__wardosKeepPdf", keep_from_hook)
        await context.add_init_script(script=f"({HOOK_SCRIPT})({json.dumps(self.origins)})")
        for page in context.pages:
            try:
                await page.evaluate(HOOK_SCRIPT, self.origins)
            except Exception:
                pass
        context.on("page", self._on_page)
        for page in context.pages:
            page.on("download", self._on_download)

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme == "blob":
            return PdfCapture._origin(url[5:])
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_page(self, page) -> None:
        page.on("download", self._on_download)
        self._spawn(self._watch_blob_tab(page))

    def _on_download(self, download) -> None:
        self._spawn(self._read_download(download))

    async def _read_download(self, download) -> None:
        try:
            path = await download.path()
            if path:
                self._keep(Path(path).read_bytes(), "download")
        except Exception:
            pass

    async def _watch_blob_tab(self, page) -> None:
        try:
            for _ in range(100):
                if page.is_closed():
                    return
                if page.url.startswith("blob:") and self._origin(page.url) in self.origins:
                    encoded = await asyncio.wait_for(page.evaluate(FETCH_BLOB), timeout=20)
                    self._keep(base64.b64decode(encoded), "blob-tab")
                    return
                await asyncio.sleep(0.2)
        except Exception:
            pass

    async def wait_for_new(self, since: int, timeout: float) -> list[CapturedPdf]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.captured) <= since:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return []
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                pass
        await asyncio.sleep(0.5)
        return self.captured[since:]
