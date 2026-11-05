"""Show-me mode: let a person watch the robot work in its own browser (WARDOS_SHOW_ME=1)."""
from __future__ import annotations

import asyncio
import os
import re

TAG = "wardos-showme"
GLIDE_S = 0.4
QUICK_GLIDE_S = 0.15
CALL_LIMIT_S = 1.0
POINT_LIMIT_S = 2.5
MAX_LINE = 70

API_JS = r"""
(() => {
  const KEY = Symbol.for('wardos.showme');
  if (window[KEY] && window[KEY].host.isConnected) return;
  const SVG = 'http://www.w3.org/2000/svg';
  const host = document.createElement('wardos-showme');
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'all:initial;display:block;position:fixed;left:0;top:0;width:0;height:0;' +
    'z-index:2147483647;pointer-events:none';
  const root = host.attachShadow({mode: 'closed'});
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('width', '26'); svg.setAttribute('height', '30'); svg.setAttribute('viewBox', '0 0 26 30');
  const arrow = document.createElementNS(SVG, 'path');
  arrow.setAttribute('d', 'M3 2 L3 24 L9 18.5 L13 27.5 L17.5 25.5 L13.5 16.8 L21.5 16.2 Z');
  arrow.setAttribute('fill', '#2F6BFF'); arrow.setAttribute('stroke', '#FFFFFF');
  arrow.setAttribute('stroke-width', '2'); arrow.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(arrow);
  const cursor = document.createElement('div');
  cursor.style.cssText = 'position:fixed;left:0;top:0;display:none;pointer-events:none;margin:-2px 0 0 -3px;' +
    'filter:drop-shadow(0 2px 3px rgba(15,26,46,.35));will-change:transform';
  cursor.appendChild(svg);
  const line = document.createElement('div');
  line.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:none;' +
    'pointer-events:none;box-sizing:border-box;max-width:min(640px,calc(100vw - 32px));padding:10px 18px;' +
    'border-radius:999px;background:#0F1A2E;color:#FFFFFF;font:500 15px/20px system-ui,-apple-system,"Segoe UI",' +
    'Roboto,Arial,sans-serif;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' +
    'box-shadow:0 6px 20px rgba(15,26,46,.28)';
  root.appendChild(cursor);
  root.appendChild(line);
  (document.documentElement || document).appendChild(host);
  const api = {host, x: null, y: null, line: '', rings: 0, hidden: 0,
    move(x, y, ms) {
      if (api.x === null) {       // first time: rise from the middle of the window
        cursor.style.transition = 'none';
        cursor.style.transform = `translate(${innerWidth / 2}px,${innerHeight / 2}px)`;
        cursor.style.display = 'block';
        cursor.getBoundingClientRect();
      }
      cursor.style.transition = ms ? `transform ${ms}ms cubic-bezier(.3,.7,.2,1)` : 'none';
      cursor.style.transform = `translate(${x}px,${y}px)`;
      cursor.style.display = 'block';
      api.x = x; api.y = y;
    },
    ring(x, y) {
      const ring = document.createElement('div');
      ring.style.cssText = `position:fixed;left:${x - 22}px;top:${y - 22}px;width:44px;height:44px;` +
        'box-sizing:border-box;border:3px solid #2F6BFF;border-radius:50%;pointer-events:none';
      root.appendChild(ring);
      api.rings++;
      const done = () => ring.remove();
      const run = ring.animate([{transform: 'scale(.35)', opacity: 1}, {transform: 'scale(1.25)', opacity: 0}],
                               {duration: 520, easing: 'ease-out', fill: 'forwards'});
      run.onfinish = done; run.oncancel = done;
    },
    say(text) {
      api.line = text;
      line.textContent = text;
      line.style.display = text ? 'block' : 'none';
    },
    hide() { api.hidden++; host.style.setProperty('display', 'none', 'important'); },
    show() { api.hidden = Math.max(0, api.hidden - 1);
             if (!api.hidden) host.style.setProperty('display', 'block'); },
    state: () => ({x: api.x, y: api.y, line: api.line, rings: api.rings, connected: host.isConnected}),
  };
  Object.defineProperty(window, KEY, {value: api, configurable: true});
})()
"""

CALL_JS = "([name, args]) => { const s = window[Symbol.for('wardos.showme')]; return s ? s[name](...args) : null; }"

RESTORE_JS = """([x, y, line]) => { const s = window[Symbol.for('wardos.showme')]; if (!s) return;
  if (x !== null) s.move(x, y, 0); s.say(line); }"""

ACTION_WORDS = {
    "click": "Clicking", "click_until": "Clicking", "fill": "Typing in a box", "check": "Ticking a box",
    "select": "Choosing from a list", "press": "Pressing a key", "goto": "Opening a page",
    "home": "Going to the home page", "wait": "Waiting for the page", "read_table": "Reading the table",
    "select_rows": "Ticking the wanted rows", "select_rows_paged": "Ticking the wanted rows",
    "capture_pdf": "Keeping the report PDF",
}
EXPLORER_WORDS = {
    "look": "Looking at the page", "click": "Clicking", "type": "Typing in a box", "select": "Choosing from a list",
    "press_enter": "Pressing Enter", "scroll_down": "Scrolling down", "scroll_up": "Scrolling up",
    "back": "Going back", "switch_tab": "Changing tab", "read_table": "Reading the table", "wait": "Waiting for the page",
}
SILENT_ACTIONS = {"end_if"}
VERBS = {"click": "Clicking", "open": "Opening", "navigate": "Going", "go": "Going", "read": "Reading", "search": "Searching",
         "type": "Typing", "typed": "Typing", "enter": "Entering", "tick": "Ticking", "select": "Selecting",
         "choose": "Choosing", "print": "Printing", "expand": "Opening", "close": "Closing", "reach": "Going to",
         "start": "Starting", "ensure": "Making sure", "stop": "Stopping", "keep": "Keeping"}


def enabled() -> bool:
    return os.environ.get("WARDOS_SHOW_ME", "").strip() == "1"


def clean_line(intent, action: str = "", values=()) -> str:
    text = str(intent or "")
    text = re.sub(r"(?<!\w)(['\"`]).*?\1(?!\w)", " ", text)
    text = re.sub(r"\{[^}]*\}|\([^)]*\)|\[[^\]]*\]", " ", text)
    for value in values:
        if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value).strip()) >= 2:
            text = re.sub(rf"(?<!\w){re.escape(str(value).strip())}(?!\w)", " ", text, flags=re.I)
    text = " ".join(word for word in text.split() if not re.search(r"\d", word)).strip(" ,.;:-'\"")
    if not re.search(r"[A-Za-z]{2}", text):
        return ACTION_WORDS.get(action, "")
    first, _, rest = text.partition(" ")
    first = VERBS.get(first.lower(), first[:1].upper() + first[1:])
    return f"{first} {rest}".strip()[:MAX_LINE]


class _Off:
    on = False

    async def step(self, step, values=None):
        return None

    async def say(self, word):
        return None

    async def point(self, locator, *, ring=True, quick=False):
        return None


OFF = _Off()


class ShowMe:
    on = True

    def __init__(self, state):
        self.state = state
        self.x = self.y = None
        self.line = ""
        self.lines: list[str] = []
        self._tasks: set = set()
        state.context.on("page", self._watch)
        for page in state.context.pages:
            self._watch(page)

    def _watch(self, page) -> None:
        page.on("domcontentloaded", self._restore_soon)

    def _restore_soon(self, page) -> None:
        if not page.url.startswith("http"):
            return
        task = asyncio.ensure_future(self._restore(page))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _restore(self, page) -> None:
        await self._quiet(self._ensure(page))

    async def _ensure(self, page) -> None:
        await page.evaluate(API_JS)
        await page.evaluate(RESTORE_JS, [self.x, self.y, self.line])

    @staticmethod
    async def _quiet(call, limit: float = CALL_LIMIT_S) -> None:
        try:
            await asyncio.wait_for(call, limit)
        except Exception:
            pass

    async def step(self, step: dict, values: dict | None = None) -> None:
        action = step.get("action", "")
        if action in SILENT_ACTIONS:
            return
        await self.say(clean_line(step.get("intent"), action, (values or {}).values()))

    async def say(self, line: str) -> None:
        if not line or line == self.line:
            return
        self.line = line
        self.lines.append(line)
        await self._quiet(self._say())

    async def _say(self) -> None:
        page = await self.state.active()
        await self._ensure(page)

    async def point(self, locator, *, ring: bool = True, quick: bool = False) -> None:
        await self._quiet(self._point(locator, ring, QUICK_GLIDE_S if quick else GLIDE_S), POINT_LIMIT_S)

    async def _point(self, locator, ring: bool, glide_s: float) -> None:
        page = locator.page
        try:
            await locator.scroll_into_view_if_needed(timeout=500)
        except Exception:
            pass
        box = await locator.bounding_box(timeout=500)
        if not box:
            return
        x, y = round(box["x"] + box["width"] / 2, 1), round(box["y"] + box["height"] / 2, 1)
        await page.evaluate(API_JS)
        await page.evaluate(CALL_JS, ["move", [x, y, int(glide_s * 1000)]])
        self.x, self.y = x, y
        await asyncio.sleep(glide_s)
        if ring:
            await page.evaluate(CALL_JS, ["ring", [x, y]])


def for_browser(state):
    if not enabled() or state is None:
        return OFF
    try:
        existing = getattr(state, "_showme", None)
        if existing is None:
            _hide_in_screenshots()
            existing = state._showme = ShowMe(state)
        return existing
    except Exception:
        return OFF


_HIDE_STYLE = f"{TAG} {{ display: none !important; }}"
_patched = False


def _hide_in_screenshots() -> None:
    global _patched
    if _patched:
        return
    from playwright.async_api import Locator, Page

    def wrap(original, page_of):
        async def screenshot(self, *args, **kwargs):
            if not enabled():
                return await original(self, *args, **kwargs)
            page = page_of(self)
            kwargs["style"] = _HIDE_STYLE + (kwargs.get("style") or "")
            await ShowMe._quiet(page.evaluate(CALL_JS, ["hide", []]))
            try:
                return await original(self, *args, **kwargs)
            finally:
                await ShowMe._quiet(page.evaluate(CALL_JS, ["show", []]))
        return screenshot

    Page.screenshot = wrap(Page.screenshot, lambda page: page)
    Locator.screenshot = wrap(Locator.screenshot, lambda locator: locator.page)
    _patched = True
