"""Explore: can the agent find its own way to a goal on the portal?"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import re
import time
from urllib.parse import urlsplit

from . import showme
from .runlog import RunLog

WRITE_WORDS = re.compile(
    r"\b(save|submit|delete|remove|discharge|confirm|approve|update|cancel|finali[sz]e|"
    r"generate\s+bill|issue|dispatch|transfer|admit|allot|allocate|book|reject|verify|authori[sz]e|"
    r"block|release|close\s+case|send)\b", re.I)
LOGOUT_WORDS = re.compile(r"log\s*-?\s*out|sign\s*-?\s*out|change\s*password|reset\s*password|^\s*log\s*-?\s*in\s*$", re.I)
ACTIONS = {"click", "type", "select", "press_enter", "scroll_down", "scroll_up", "back", "switch_tab",
           "read_table", "wait", "done", "give_up"}

TABLE_JS = r"""
(maxRows) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const short = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  const tables = Array.from(document.querySelectorAll('table')).filter(vis);
  if (!tables.length) return null;
  const t = tables.sort((a, b) => b.querySelectorAll('tr').length - a.querySelectorAll('tr').length)[0];
  const headers = Array.from(t.querySelectorAll('thead th')).map(h => short(h.innerText));
  const rows = Array.from(t.querySelectorAll('tbody tr')).filter(vis)
    .map(tr => Array.from(tr.querySelectorAll('td')).map(td => short(td.innerText)));
  const foot = Array.from(t.querySelectorAll('tfoot tr')).map(tr => Array.from(tr.querySelectorAll('td, th')).map(c => short(c.innerText)));
  const pager = Array.from(document.querySelectorAll('.mat-paginator-range-label, .pagination, .dataTables_info'))
    .filter(vis).map(e => short(e.innerText)).join(' | ');
  return {headers, rows: rows.slice(0, maxRows), total_rows: rows.length, foot, pager};
}
"""
LOADER_TEXT = re.compile(r"please\s+wait|loading", re.I)

OBSERVE_JS = r"""
(maxItems) => {
  const vis = e => {
    if (!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };
  const short = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n || 70);
  document.querySelectorAll('[data-wardos-n]').forEach(e => e.removeAttribute('data-wardos-n'));
  const sel = 'a, button, input, select, textarea, [role=button], [role=link], [role=tab], [role=menuitem], ' +
              '[role=option], [role=checkbox], [role=radio], [role=combobox], mat-select, mat-option, ' +
              '[routerlink], [tabindex]:not([tabindex="-1"])';
  const chosen = new Map();
  const out = [];
  // Standard controls first, then anything the mouse pointer turns into a hand over (how a person
  // spots clickable menu headings and tiles), outermost only.
  const pointer = Array.from(document.querySelectorAll('div, span, li, i, mat-card, h3, h4, h5, p, img'))
    .filter(e => getComputedStyle(e).cursor === 'pointer' && !e.closest(sel)
                 && !(e.parentElement && getComputedStyle(e.parentElement).cursor === 'pointer'));
  for (const e of [...document.querySelectorAll(sel), ...pointer]) {
    if (out.length >= maxItems) break;
    if (!vis(e) || e.closest('[data-wardos-overlay]')) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    let label = '';
    const item = {tag, role: e.getAttribute('role') || '', type};
    if (tag === 'input' || tag === 'textarea') {
      const lab = e.id ? document.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null;
      label = short((lab && lab.innerText) || e.getAttribute('aria-label') || e.getAttribute('placeholder')
                    || e.getAttribute('formcontrolname') || e.getAttribute('name'));
      if (type === 'checkbox' || type === 'radio') item.checked = e.checked;
      else if (type !== 'password') item.has_value = !!e.value;        // never the value itself
    } else if (tag === 'select') {
      label = short(e.getAttribute('aria-label') || e.getAttribute('formcontrolname') || e.getAttribute('name'));
      item.options = Array.from(e.options).slice(0, 25).map(o => short(o.label || o.textContent, 40));
      item.selected = short(e.options[e.selectedIndex] && e.options[e.selectedIndex].text, 40);
    } else {
      const img = e.querySelector('img');
      label = short(e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || (img && img.alt) || '');
      if (!label) {
        const icon = e.querySelector('i, mat-icon, span[class*=icon]');
        if (icon) label = '(icon ' + short(icon.className || icon.innerText, 30) + ')';
      }
    }
    // skip a child that repeats its already-listed parent (e.g. <span> inside a menu <a>)
    let dup = false;
    for (let p = e.parentElement, d = 0; p && d < 5; p = p.parentElement, d++) {
      if (chosen.has(p) && chosen.get(p) === label) { dup = true; break; }
    }
    if (dup) continue;
    const n = out.length + 1;
    e.setAttribute('data-wardos-n', String(n));
    chosen.set(e, label);
    item.n = n;
    item.label = label;
    if (tag === 'a') item.href = (e.getAttribute('href') || '').replace(location.origin, '').slice(0, 60);
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if (e.getAttribute('aria-expanded')) item.expanded = e.getAttribute('aria-expanded');
    const cell = e.closest('td');
    if (cell && cell.parentElement) {
      const table = cell.closest('table');
      const idx = Array.from(cell.parentElement.children).indexOf(cell);
      const ths = table ? Array.from(table.querySelectorAll('thead th')) : [];
      item.table_column = ths[idx] ? short(ths[idx].innerText, 30) : ('col' + idx);
      item.table_row = Array.from(cell.parentElement.parentElement.children).indexOf(cell.parentElement) + 1;
    }
    item.zone = e.closest('.modal, .swal2-popup, [role=dialog], .cdk-overlay-pane') ? 'dialog'
              : e.closest('nav, aside, .sidebar, [class*=sidebar], [class*=side-nav], [class*=sidenav]') ? 'menu'
              : e.closest('header, .navbar, .header') ? 'header' : 'main';
    item.in_view = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    out.push(item);
  }
  const main = document.querySelector('main, .main-content, .content-wrapper, .page-content, mat-sidenav-content') || document.body;
  return {path: location.pathname, title: document.title, elements: out,
          text: short(main.innerText, 2500),
          loading: Array.from(document.querySelectorAll('.loader, .spinner, .ngx-spinner-overlay, [class*=loading]')).some(vis),
          can_scroll_down: (window.scrollY + innerHeight + 20) < document.documentElement.scrollHeight};
}
"""

OVERLAY_JS = r"""
(on) => {
  document.querySelectorAll('[data-wardos-overlay]').forEach(e => e.remove());
  if (!on) return 0;
  const box = document.createElement('div');
  box.setAttribute('data-wardos-overlay', '1');
  box.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
  let count = 0;
  for (const e of document.querySelectorAll('[data-wardos-n]')) {
    const r = e.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const tag = document.createElement('div');
    tag.textContent = e.getAttribute('data-wardos-n');
    tag.style.cssText = `position:fixed;left:${Math.max(0, r.left)}px;top:${Math.max(0, r.top)}px;` +
      'background:#ffeb3b;color:#000;font:bold 11px/13px Arial;padding:0 2px;border:1px solid #000;pointer-events:none';
    box.appendChild(tag);
    count++;
  }
  document.body.appendChild(box);
  return count;
}
"""

DESCRIBE_JS = r"""
(n) => {
  const el = document.querySelector(`[data-wardos-n="${n}"]`);
  if (!el) return null;
  const short = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const tag = el.tagName.toLowerCase();
  // Ids the page makes up on each load (mat-select-20, mat-option-316, cdk-..., af22205e4dd3-1) are
  // different on replay (live 23 Oct): never use them. A form field's formcontrolname is stable.
  const madeUp = id => /^(mat-|cdk-|ng-)|\d{2,}|^[a-f0-9]{6,}/i.test(id);
  const cssPath = e0 => {
    const parts = [];
    for (let e = e0, d = 0; e && e.nodeType === 1 && d < 4; e = e.parentElement, d++) {
      let p = e.tagName.toLowerCase();
      const fcn = e.getAttribute('formcontrolname');
      if (fcn) { parts.unshift(`${p}[formcontrolname='${fcn}']`); break; }
      if (e.id && !madeUp(e.id)) { p += '#' + e.id; parts.unshift(p); break; }
      const cls = (typeof e.className === 'string' ? e.className : '').trim().split(/\s+/)
                    .filter(c => c && !c.startsWith('ng-') && !c.startsWith('cdk-focused')).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
    }
    return parts.join(' > ');
  };
  const info = {tag, id: el.id || '', name: el.getAttribute('name') || '', fcn: el.getAttribute('formcontrolname') || '',
    type: el.getAttribute('type') || '', role: el.getAttribute('role') || '', aria: el.getAttribute('aria-label') || '',
    title: el.getAttribute('title') || '', href: tag === 'a' ? (el.getAttribute('href') || '') : '', css: cssPath(el),
    path: location.pathname};
  // Look-alikes (e.g. the FROM and TO date boxes' 'Open calendar' buttons share one css): which visible one it was.
  try {
    const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    const same = Array.from(document.querySelectorAll(info.css)).filter(shown);
    info.css_count = same.length; info.css_index = same.indexOf(el);
  } catch (e) {}
  const cell = el.closest('td');
  if (cell) {
    const table = cell.closest('table');
    const idx = Array.from(cell.parentElement.children).indexOf(cell);
    const ths = table ? Array.from(table.querySelectorAll('thead th')) : [];
    info.in_table = true; info.column = ths[idx] ? short(ths[idx].innerText) : ('col' + idx);
    info.columns = ths.map(h => short(h.innerText));
    info.row_has_checkbox = !!cell.parentElement.querySelector('input[type=checkbox]');
  } else if (!['input', 'textarea'].includes(tag)) {
    info.text = short(el.innerText || el.textContent);
    if (!info.text) {
      // An unlabeled icon (e.g. a menu's expand arrow): record the labelled box it sits in, so a
      // compiled skill can target "the menu heading X" instead of a bare icon.
      for (let p = el.parentElement, d = 0; p && d < 3; p = p.parentElement, d++) {
        const t = short(p.innerText);
        if (t && t.length <= 40) { info.near_text = t; info.near_css = cssPath(p); break; }
      }
    }
  } else {
    info.placeholder = el.getAttribute('placeholder') || '';
    const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    info.label = lab ? short(lab.innerText) : '';
  }
  return info;
}
"""


@dataclass
class Step:
    number: int
    action: str
    element: int | None
    described: str
    result: str
    page_after: str
    seconds: float
    model_seconds: float
    thought: str = ""


@dataclass
class ExploreResult:
    goal: str
    status: str
    answer: str
    verified: str
    steps: list[Step] = field(default_factory=list)
    seconds: float = 0.0
    model_seconds: float = 0.0
    blocked: int = 0
    trace: list[dict] = field(default_factory=list)
    final_path: str = ""
    pending: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def mask_digits(text: str) -> str:
    return re.sub(r"\d{4,}", "{n}", text or "")


def safe_label(item: dict) -> str:
    if item.get("table_column"):
        return f"table row {item.get('table_row')}, column '{item['table_column']}'"
    return mask_digits(item.get("label") or "")[:50]


def kind_of(item: dict) -> str:
    tag, role, typ = item.get("tag", ""), item.get("role", ""), item.get("type", "")
    if tag == "input":
        return {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button"}.get(typ, "textbox")
    if tag == "select" or role == "combobox" or tag == "mat-select":
        return "dropdown"
    if tag == "textarea":
        return "textbox"
    if tag == "a" or role == "link":
        return "link"
    if tag == "button" or role == "button":
        return "button"
    return role or tag


def element_lines(elements: list[dict], limit: int = 160) -> list[str]:
    lines = []
    for item in elements[:limit]:
        extra = []
        if item.get("zone") != "main":
            extra.append(item["zone"])
        if item.get("table_column"):
            extra.append(f"table row {item.get('table_row')} col '{item['table_column']}'")
        if item.get("expanded"):
            extra.append(f"expanded={item['expanded']}")
        if item.get("checked") is not None:
            extra.append(f"checked={item['checked']}")
        if item.get("has_value"):
            extra.append("has text")
        if item.get("disabled"):
            extra.append("disabled")
        if item.get("options"):
            extra.append(f"options={item['options']}, selected='{item.get('selected', '')}'")
        if item.get("href"):
            extra.append(f"href={item['href']}")
        if not item.get("in_view"):
            extra.append("off-screen")
        lines.append(f"[{item['n']}] {kind_of(item)} '{item.get('label', '')}'" + (f"  ({'; '.join(extra)})" if extra else ""))
    return lines


def is_blocked(item: dict) -> str | None:
    label = item.get("label") or ""
    if LOGOUT_WORDS.search(label):
        return "logout/password controls are off-limits"
    if kind_of(item) == "button" and item.get("zone") != "menu" and WRITE_WORDS.search(label):
        return f"'{label[:30]}' would change data; the agent stops here for the doctor's approval"
    return None


def _number(text: str) -> float | None:
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def column_totals(headers: list[str], rows: list[list[str]]) -> dict[str, float]:
    totals = {}
    for index, header in enumerate(headers):
        cells = [row[index] for row in rows if index < len(row) and str(row[index]).strip()]
        numbers = [n for n in (_number(c) for c in cells) if n is not None]
        if cells and len(numbers) >= 0.8 * len(cells) and header and not re.search(r"\b(s\.?\s*no|sl|sr|#)\b", header, re.I):
            total = sum(numbers)
            totals[header] = int(total) if total == int(total) else round(total, 2)
    return totals


def table_complete(table: dict) -> bool:
    rows = table.get("rows") or []
    if table.get("total_rows", len(rows)) != len(rows):
        return False
    pager = (table.get("pager") or "").replace(",", "")
    match = re.search(r"(\d+)\s*(?:[-–]|to)\s*(\d+)\s+(?:of|/)\s*(\d+)", pager, re.I)
    if match:
        return int(match[1]) <= 1 and int(match[2]) >= int(match[3])
    return not bool(re.search(r"\b(?:[2-9]|\d{2,})\b|\bnext\b|\bof\b|[›»/]", pager, re.I))


def table_text(table: dict | None, max_chars: int = 12000) -> str:
    if not table:
        return "(no table on this page)"
    lines = [" | ".join(table["headers"])] + [" | ".join(row) for row in table["rows"]]
    lines += ["FOOTER: " + " | ".join(row) for row in table.get("foot") or []]
    totals = column_totals(table["headers"], table["rows"])
    if totals:
        lines.append("COLUMN TOTALS OF THE ROWS ABOVE (added up by code, exact): "
                     + "; ".join(f"{k} = {v}" for k, v in totals.items()))
    if table.get("pager"):
        lines.append(f"PAGER: {table['pager']}")
    lines.append(f"({len(table['rows'])} of {table['total_rows']} visible rows shown)")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n(... cut)"


def build_prompt(goal: str, hint: str | None, site_map: str | None, observation: dict, history: list[str],
                 tabs: list[str], tab_index: int, nudge: str | None, table_data: str | None = None) -> str:
    parts = [
        "You operate a hospital information system website for a doctor, using its normal screens like a careful "
        "new staff member. The account is already logged in.",
        f"\nGOAL: {goal}",
    ]
    if hint:
        parts.append(f"HINT FROM THE DOCTOR: {hint}")
    if site_map:
        parts.append("MAP OF THIS WEBSITE (screens found by an earlier survey):\n" + site_map)
    parts.append(
        "\nRULES\n"
        "- Read-only. Never save, submit, discharge, delete, approve, cancel or change anything. If the goal needs such "
        "a button, fill what is needed, do NOT press it, and finish with action 'done' saying the form is ready for "
        "the doctor's approval.\n"
        "- Never log out, never open profile or password settings.\n"
        "- Use menus and links. A menu heading may need a click to show its items.\n"
        "- To read, count or add up a table, use action 'read_table' (gives every row of the biggest visible table "
        "as text). Do not scroll through tables to count. If the table has pages, set the largest page size first.\n"
        "- After 3 tries on one path without progress, go back and try a different menu.\n"
        "- When the goal is reached (the answer is on screen or the requested screen is open), finish with 'done' and "
        "give the answer exactly as the screen shows it, and name the screen.\n"
        "- If this account cannot do it, finish with 'give_up' and say why.")
    if table_data:
        parts.append("TABLE YOU READ IN THE LAST STEP:\n" + table_data)
    parts.append(f"\nCURRENT PAGE: path {observation['path']} | title '{observation['title']}' | tab {tab_index + 1} of {len(tabs)}"
                 + (" | the page shows a loading indicator" if observation.get("loading") else "")
                 + (" | more content below (scroll_down)" if observation.get("can_scroll_down") else ""))
    if len(tabs) > 1:
        parts.append("TABS: " + "; ".join(f"{i}: {t}" for i, t in enumerate(tabs)))
    parts.append("STEPS SO FAR:\n" + ("\n".join(history[-15:]) if history else "(none)"))
    if nudge:
        parts.append(f"NOTE: {nudge}")
    parts.append("VISIBLE TEXT (main area, shortened):\n" + (observation.get("text") or "")[:2500])
    parts.append("CLICKABLE THINGS (the screenshot shows the same numbers on yellow tags):\n"
                 + "\n".join(element_lines(observation["elements"])))
    parts.append(
        '\nReply with ONE JSON object: {"observation": "what you see, short", "thought": "why the next action", '
        '"action": "click|type|select|press_enter|scroll_down|scroll_up|back|switch_tab|read_table|wait|done|give_up", '
        '"element": <number or null>, "text": "<text to type>", "option": "<option to choose>", '
        '"tab": <tab index>, "answer": "<for done/give_up>"}')
    return "\n".join(parts)


def parse_decision(reply: dict) -> dict:
    action = str(reply.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    element = reply.get("element")
    try:
        element = int(element) if element not in (None, "", "null") else None
    except (TypeError, ValueError):
        element = None
    if action in {"click", "type", "select"} and element is None:
        raise ValueError(f"action {action} needs an element number")
    return {"action": action, "element": element, "text": str(reply.get("text") or ""),
            "option": str(reply.get("option") or ""), "tab": reply.get("tab"),
            "answer": str(reply.get("answer") or ""), "thought": str(reply.get("thought") or "")[:300]}


DATE_VALUE = re.compile(r"^\d{2}/\d{2}/\d{4}(?: \d{2}:\d{2}(?::\d{2})?)?$")


def value_ref(text: str, inputs: dict) -> str:
    names = [name for name, value in inputs.items() if value and text.strip() == str(value).strip()]
    if not names:
        digits = re.sub(r"\D", "", text)
        names = [name for name, value in inputs.items() if digits and DATE_VALUE.match(str(value or "").strip())
                 and digits in {re.sub(r"\D", "", str(value)), re.sub(r"\D", "", str(value).split()[0])}]
    if names:
        return "{" + "|".join(names) + "}"
    return "(not recorded)" if re.search(r"\d{4,}", text) else text[:40]


class Explorer:
    def __init__(self, keeper, models, log: RunLog, *, max_steps: int = 40, minutes: float = 10,
                 private_console: bool = False):
        self.keeper = keeper
        self.browser = keeper.browser
        self.models = models
        self.log = log
        self.max_steps = max_steps
        self.minutes = minutes
        self.private = private_console
        self.table_data: str | None = None
        self.table_partial = False
        self.last_blocked: dict | None = None
        self.show = showme.for_browser(self.browser)

    def _say(self, message: str):
        self.log.say(mask_digits(message))

    async def _observe(self, page) -> tuple[dict, bytes]:
        await self.show.say(showme.EXPLORER_WORDS["look"])
        observation = await page.evaluate(OBSERVE_JS, 180)
        await page.evaluate(OVERLAY_JS, True)
        try:
            shot = await page.screenshot(type="jpeg", quality=60, timeout=10000)
        finally:
            await page.evaluate(OVERLAY_JS, False)
        return observation, shot

    async def _settle(self, page):
        await self.browser.settle()

    async def _act(self, decision: dict, observation: dict, inputs: dict, trace: list[dict]) -> str:
        page = await self.browser.active()
        action, number = decision["action"], decision["element"]
        item = next((e for e in observation["elements"] if e["n"] == number), None) if number else None
        if action in {"click", "type", "select"} and item is None:
            return f"failed: there is no element [{number}] on this page"
        locator = page.locator(f'[data-wardos-n="{number}"]').first if number else None
        described = await page.evaluate(DESCRIBE_JS, number) if number else None
        await self.show.say(showme.EXPLORER_WORDS.get(action, ""))
        if action == "click":
            reason = is_blocked(item)
            if reason:
                if described and not LOGOUT_WORDS.search(item.get("label") or ""):
                    self.last_blocked = {"label": safe_label(item), "path": observation["path"], "element": described}
                return f"blocked: {reason}"
            before = set(self.browser.context.pages)
            await self.show.point(locator)
            await locator.click(timeout=8000)
            await asyncio.sleep(0.4)
            fresh = [p for p in self.browser.context.pages if p not in before and not p.is_closed()
                     and not p.url.startswith("blob:")]
            if fresh:
                self.browser.page = fresh[-1]
                try:
                    await fresh[-1].wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
            opener = getattr(self, "_failed_select", None)
            is_option = item.get("role") == "option" or item.get("tag") == "mat-option"
            last = trace[-1] if trace else None
            if is_option and not opener and last and last.get("kind") == "click" and (
                    last.get("tag") == "mat-select" or last.get("role") in {"combobox", "listbox"}):
                opener = {k: v for k, v in trace.pop().items() if k != "kind"}
            if opener and is_option:
                trace.append({"kind": "choose", **opener, "option": (item.get("label") or "").strip()})
            elif described:
                trace.append({"kind": "click", **described})
            self._failed_select = None
            return "ok" + (" (a new tab opened and is now active)" if fresh else "")
        if action == "type":
            if item.get("type") == "password":
                return "blocked: password fields are off-limits"
            await self.show.point(locator, ring=False)
            await locator.fill(decision["text"], timeout=8000)
            if described:
                trace.append({"kind": "typed", **described, "value": value_ref(decision["text"], inputs)})
            return "ok"
        if action == "select":
            await self.show.point(locator)
            wanted = decision["option"]
            if item.get("tag") == "select":
                options = item.get("options") or []
                label = next((o for o in options if o.strip().lower() == wanted.strip().lower()), None) \
                    or next((o for o in options if wanted.strip().lower() in o.lower()), None)
                if label is None:
                    return f"failed: no option like '{wanted}'"
                await locator.select_option(label=label, timeout=8000)
            else:
                try:
                    await locator.click(timeout=8000, force=True)
                    await page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(wanted.strip())}\s*$", re.I)
                                           ).first.click(timeout=8000)
                except Exception:
                    self._failed_select = described
                    raise
            if described:
                trace.append({"kind": "choose", **described, "option": wanted})
            return "ok"
        if action == "press_enter":
            await page.keyboard.press("Enter")
            trace.append({"kind": "enter", "path": observation["path"]})
            return "ok"
        if action in {"scroll_down", "scroll_up"}:
            await page.mouse.wheel(0, 650 if action == "scroll_down" else -650)
            return "ok"
        if action == "back":
            await page.go_back(timeout=15000)
            return "ok"
        if action == "switch_tab":
            pages = [p for p in self.browser.context.pages if not p.is_closed()]
            try:
                self.browser.page = pages[int(decision["tab"])]
                await self.browser.page.bring_to_front()
            except (TypeError, ValueError, IndexError):
                return "failed: no such tab"
            return "ok"
        if action == "read_table":
            table = await page.evaluate(TABLE_JS, 300)
            self.table_data = table_text(table)
            self.table_partial = bool(table) and not table_complete(table)
            trace.append({"kind": "read_table", "path": observation["path"],
                          "columns": table["headers"] if table else []})
            if not table:
                return "no table on this page"
            if self.table_partial:
                return (f"ok: read {table['total_rows']} row(s), BUT this is only part of the list (pager: "
                        f"'{table.get('pager') or 'more pages'}'). Set the largest page size / items per page, "
                        "then read_table again before done.")
            return f"ok: read {table['total_rows']} row(s)"
        if action == "wait":
            await asyncio.sleep(2.5)
            return "ok"
        return "ok"

    async def run(self, goal: str, *, hint: str | None = None, site_map: str | None = None,
                  inputs: dict | None = None, expect_text: str | None = None,
                  expect_url: str | None = None, shown_goal: str | None = None,
                  start_here: bool = False) -> ExploreResult:
        inputs = inputs or {}
        started = time.monotonic()
        deadline = started + self.minutes * 60
        result = ExploreResult(goal=shown_goal or goal, status="ERROR", answer="", verified="UNCHECKED")
        portal = self.keeper.portal
        page = await self.browser.active() if start_here else await portal.go_home(self.browser, log=self.log)
        await self._settle(page)
        history: list[str] = []
        repeats: dict[str, int] = {}
        last_key = ""
        table_path = None
        partial_refused = False
        self.table_partial = False
        self._say(f"Goal: {shown_goal or goal}" + (f"  | hint: {hint}" if hint else "") + ("  | with map" if site_map else ""))
        for number in range(1, self.max_steps + 1):
            if time.monotonic() >= deadline:
                result.status = "OUT_OF_TIME"
                break
            step_started = time.monotonic()
            page = await self.browser.active()
            try:
                observation, shot = await self._observe(page)
            except Exception as exc:
                history.append(f"{number}. (could not read the page: {type(exc).__name__})")
                await asyncio.sleep(1.5)
                continue
            tabs = [f"{p.url.split('?')[0][-50:]}" for p in self.browser.context.pages if not p.is_closed()]
            tab_index = next((i for i, p in enumerate(p for p in self.browser.context.pages if not p.is_closed())
                              if p is page), 0)
            nudge = None
            if repeats.get(last_key, 0) >= 3:
                nudge = "You repeated the same action 3 times without the page changing. Try a different path."
            if table_path is not None and table_path != observation["path"]:
                self.table_data, table_path = None, None
                self.table_partial = False
            prompt = build_prompt(goal, hint, site_map, observation, history, tabs, tab_index, nudge, self.table_data)
            model_started = time.monotonic()
            decision = None
            for attempt in range(2):
                try:
                    reply = await self.models.json(prompt, images=[(shot, "image/jpeg")], timeout=75)
                    decision = parse_decision(reply)
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:120]}"
                    prompt += f"\nYour previous reply could not be used ({error}). Reply with the JSON object only."
            model_seconds = round(time.monotonic() - model_started, 2)
            result.model_seconds += model_seconds
            if decision is None:
                history.append(f"{number}. (no usable decision from the model)")
                continue
            item = next((e for e in observation["elements"] if e["n"] == decision["element"]), None) \
                if decision["element"] else None
            described = f"[{decision['element']}] {kind_of(item)} '{safe_label(item)}'" if item else ""
            if decision["action"] == "done" and not partial_refused:
                try:
                    shown = await page.evaluate(TABLE_JS, 300)
                except Exception:
                    shown = None
                partial = self.table_partial or bool(shown and shown.get("rows") and not table_complete(shown))
            else:
                partial = False
            if partial:
                partial_refused = True
                history.append(f"{number}. done -> refused: the table you read is only part of the list. Show all "
                               "rows (largest page size) and read_table again first.")
                continue
            if decision["action"] in {"done", "give_up"}:
                result.status = "DONE" if decision["action"] == "done" else "GAVE_UP"
                result.answer = decision["answer"]
                result.steps.append(Step(number, decision["action"], None, "", "finished", observation["path"],
                                         round(time.monotonic() - step_started, 2), model_seconds, decision["thought"]))
                self._say(f"step {number}: {decision['action'].upper()}"
                          + ("" if self.private else f" - {decision['thought'][:160]}"))
                break
            recorded = len(result.trace)
            acted = time.monotonic()
            try:
                outcome = await self._act(decision, observation, inputs, result.trace)
            except Exception as exc:
                outcome = f"failed: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            if outcome.startswith("blocked"):
                result.blocked += 1
            if decision["action"] == "read_table":
                table_path = observation["path"]
            page = await self.browser.active()
            await self._settle(page)
            if await portal.looks_logged_out(page):
                try:
                    page = await portal.go_home(self.browser, log=self.log)
                    await self._settle(page)
                    outcome += "; that left the logged-in area, so you were put back on the dashboard"
                except Exception:
                    result.status = "SESSION_LOST"
                    history.append(f"{number}. {decision['action']} {described} -> left the logged-in area")
                    break
            path_after = page.url.replace(portal.home_url.rstrip("/"), "")[:60]
            for entry in result.trace[recorded:]:
                entry["after_path"] = urlsplit(page.url).path
                entry["t"] = round(acted - started, 2)
                entry["took"] = round(time.monotonic() - acted, 2)
            last_key = f"{decision['action']}|{described}|{observation['path']}"
            repeats[last_key] = (repeats.get(last_key, 0) + 1) if observation["path"] in path_after else 0
            entry = (f"{number}. {decision['action']} {described}".rstrip()
                     + (f" text='{decision['text'][:30]}'" if decision["action"] == "type" else "")
                     + (f" option='{decision['option'][:30]}'" if decision["action"] == "select" else "")
                     + f" -> {outcome}; page now {path_after}")
            history.append(entry)
            step = Step(number, decision["action"], decision["element"], mask_digits(described), outcome,
                        mask_digits(path_after), round(time.monotonic() - step_started, 2), model_seconds,
                        decision["thought"])
            result.steps.append(step)
            self.log.event("explore_step", **{k: v for k, v in asdict(step).items() if k != "thought"})
            self._say(f"step {number}: {decision['action']} {described} -> {outcome} ({step.seconds}s)"
                      + ("" if self.private else f"\n           why: {decision['thought'][:160]}"))
        else:
            result.status = "OUT_OF_STEPS"
        page = await self.browser.active()
        result.final_path = mask_digits(page.url.replace(self.keeper.portal.home_url.rstrip("/"), ""))
        if self.last_blocked and urlsplit(page.url).path == self.last_blocked["path"]:
            result.pending = self.last_blocked
        if expect_text or expect_url:
            ok = True
            if expect_url:
                ok = ok and expect_url in page.url
            if expect_text:
                try:
                    body = await page.locator("body").inner_text(timeout=5000)
                except Exception:
                    body = ""
                ok = ok and bool(re.search(expect_text, body, re.I))
            result.verified = "PASS" if ok else "FAIL"
        result.seconds = round(time.monotonic() - started, 2)
        result.model_seconds = round(result.model_seconds, 2)
        return result


def map_text(catalog: dict, limit: int = 120) -> str:
    lines = []
    for module in catalog.get("modules", []):
        links = [l for l in module.get("links", []) if l.get("zone") != "header"]
        if not links:
            continue
        groups: dict[str, list[str]] = {}
        for link in links:
            groups.setdefault(link.get("group") or "", []).append(link["text"])
        parts = [(f"{g}: " if g else "") + ", ".join(dict.fromkeys(items)) for g, items in groups.items()]
        lines.append(f"- {module['tile']} -> " + " | ".join(parts))
        if len(lines) >= limit:
            break
    return "\n".join(lines)
