"""Date boxes on any website: set a date, then read the box back like a person glancing at it."""
from __future__ import annotations

import asyncio
from datetime import datetime
import re

DATE_VALUE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})(?: (\d{2}):(\d{2})(?::(\d{2}))?)?$")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
SHOWN = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
TIME = re.compile(r"^\D{0,3}(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?", re.I)

POPUP = (".cdk-overlay-pane, .mat-datepicker-content, ngx-mat-datetime-content, .flatpickr-calendar.open, "
         ".ui-datepicker, .datepicker-dropdown, .daterangepicker, [role=dialog]")


def parse_value(value: str) -> datetime | None:
    match = DATE_VALUE.match(str(value or "").strip())
    if not match:
        return None
    d, mo, y, hh, mm, ss = match.groups()
    return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))


def shows(text: str, when: datetime) -> bool:
    for match in SHOWN.finditer(text or ""):
        if match.group(1):
            day, month, year = int(match[1]), int(match[2]), int(match[3])
        else:
            year, month, day = int(match[4]), int(match[5]), int(match[6])
        if (year, month, day) != (when.year, when.month, when.day):
            continue
        clock = TIME.match(text[match.end():])
        if not clock:
            return True
        hour = int(clock[1])
        if clock[4]:
            hour = hour % 12 + (12 if clock[4].lower() == "pm" else 0)
        return (hour, int(clock[2])) == (when.hour, when.minute)
    return False


def on_page(text: str, when: datetime) -> bool:
    month = MONTHS[when.month - 1]
    patterns = [rf"(?<!\d){when.day:02d}[/-]{when.month:02d}[/-]{when.year}(?!\d)",
                rf"(?<!\d){when.day}[/-]{when.month}[/-]{when.year}(?!\d)",
                rf"(?<!\d){when.year}-{when.month:02d}-{when.day:02d}(?!\d)",
                rf"(?<!\d){when.day}\s+{month[:3]}[a-z]*\.?,?\s+{when.year}", rf"{month[:3]}[a-z]*\.?\s+{when.day},?\s+{when.year}"]
    return any(re.search(p, text or "", re.I) for p in patterns)


ACCEPTED_JS = r"""(el) => !(el.getAttribute('aria-invalid') === 'true' || el.classList.contains('ng-invalid')
  || el.closest('.mat-form-field-invalid, .is-invalid, .has-error'))"""

TOGGLE_JS = r"""(el) => {
  document.querySelectorAll('[data-wardos-cal]').forEach(e => e.removeAttribute('data-wardos-cal'));
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const named = b => /calendar|date|pick/i.test([b.getAttribute('aria-label'), b.getAttribute('title'),
                                                 typeof b.className === 'string' ? b.className : ''].join(' '));
  for (let p = el.parentElement, d = 0; p && d < 5; p = p.parentElement, d++) {
    const b = Array.from(p.querySelectorAll('button, [role=button], .input-group-text, i'))
      .find(x => x !== el && vis(x) && named(x));
    if (b) { b.setAttribute('data-wardos-cal', '1'); return true; }
  }
  return false;
}"""

STEP_JS = r"""(a) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const pops = Array.from(document.querySelectorAll(a.popup)).filter(vis);
  const pop = pops[pops.length - 1];
  if (!pop) return 'no-calendar';
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const named = Array.from(pop.querySelectorAll('[aria-label], [data-date], [title]')).filter(vis).find(e => {
    const t = e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('data-date') || '';
    return a.labels.some(l => new RegExp('(^|[^0-9])' + esc(l) + '([^0-9]|$)', 'i').test(t));
  });
  if (named) {
    if (named.getAttribute('aria-disabled') === 'true' || /disabled/.test(named.className)) return 'disabled';
    (named.closest('button, td, [role=gridcell]') || named).click(); return 'clicked';
  }
  const head = (pop.innerText || '').match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})/i);
  if (!head) return 'no-month';
  const shown = parseInt(head[2]) * 12 + ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']
    .indexOf(head[1].toLowerCase().slice(0, 3));
  const want = a.y * 12 + a.m - 1;
  if (shown === want) {
    const days = Array.from(pop.querySelectorAll('button, td, [role=gridcell], a, span, div')).filter(e => vis(e)
      && (e.innerText || '').trim() === String(a.d) && !e.querySelector('button, td, a')
      && !/disabled|other|adjacent|prev-?month|next-?month|outside|old|new/i.test(
           (typeof e.className === 'string' ? e.className : '') + ' ' + ((e.parentElement || {}).className || ''))
      && e.getAttribute('aria-disabled') !== 'true');
    if (days.length >= 1) { (days[0].closest('button, td, [role=gridcell], a') || days[0]).click(); return 'clicked'; }
    return 'no-day';
  }
  const forward = want > shown;
  const arrow = Array.from(pop.querySelectorAll('button, a, [role=button], span, i')).filter(vis).find(e => {
    const t = [e.getAttribute('aria-label'), e.getAttribute('title'), typeof e.className === 'string' ? e.className : '',
               (e.innerText || '').trim()].join(' ').toLowerCase();
    return !/year/.test(t) && (forward ? /next|›|»|forward/.test(t) : /prev|‹|«|back/.test(t));
  });
  if (!arrow) return 'no-arrow';
  arrow.click(); return 'moved';
}"""

TIME_JS = r"""(popup) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const pops = Array.from(document.querySelectorAll(popup)).filter(vis);
  const pop = pops[pops.length - 1];
  if (!pop) return 0;
  const boxes = Array.from(pop.querySelectorAll('input')).filter(vis);
  let marked = 0;
  for (const box of boxes) {
    const n = [box.getAttribute('formcontrolname'), box.getAttribute('aria-label'), box.getAttribute('placeholder'),
               box.name].join(' ').toLowerCase();
    const key = /hour|\bhh\b/.test(n) ? 'hour' : /minute|\bmin\b|\bmm\b/.test(n) ? 'minute'
              : /second|\bsec\b|\bss\b/.test(n) ? 'second' : '';
    if (key) { box.setAttribute('data-wardos-time', key); marked++; }
  }
  if (!marked) {
    const small = boxes.filter(b => b.type === 'number' || b.maxLength === 2);
    if (small.length === 2 || small.length === 3)
      small.forEach((b, i) => { b.setAttribute('data-wardos-time', ['hour', 'minute', 'second'][i]); marked++; });
  }
  return marked;
}"""

CONFIRM_JS = r"""(popup) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const pops = Array.from(document.querySelectorAll(popup)).filter(vis);
  const pop = pops[pops.length - 1];
  if (!pop) return 'closed';
  const ok = Array.from(pop.querySelectorAll('button, [role=button]')).filter(vis)
    .find(b => /^\s*(done|ok|set|apply|select|choose|close)\s*$/i.test(b.innerText || b.getAttribute('aria-label') || ''));
  if (!ok) return 'none';
  ok.click(); return 'confirmed';
}"""


def day_labels(when: datetime) -> list[str]:
    full = MONTHS[when.month - 1].capitalize()
    short = full[:3]
    return [f"{when.day} {full} {when.year}", f"{full} {when.day}, {when.year}", f"{when.day} {short} {when.year}",
            f"{short} {when.day}, {when.year}", f"{when.day:02d}/{when.month:02d}/{when.year}",
            f"{when.year}-{when.month:02d}-{when.day:02d}", f"{when.day:02d}-{when.month:02d}-{when.year}"]


async def box_shows(locator, when: datetime) -> bool:
    try:
        text = await locator.input_value(timeout=2000)
        accepted = await locator.evaluate(ACCEPTED_JS)
    except Exception:
        return False
    return accepted and shows(text, when)


async def pick_in_calendar(page, locator, when: datetime, *, settle, log) -> bool:
    if await locator.evaluate(TOGGLE_JS):
        await page.locator('[data-wardos-cal="1"]').first.click(timeout=8000)
    else:
        await locator.click(timeout=8000)
    await asyncio.sleep(0.5)
    args = {"popup": POPUP, "labels": day_labels(when), "y": when.year, "m": when.month, "d": when.day}
    for _ in range(40):
        outcome = await page.evaluate(STEP_JS, args)
        if outcome == "clicked":
            break
        if outcome != "moved":
            log.event("calendar_stuck", outcome=outcome)
            return False
        await asyncio.sleep(0.3)
    else:
        return False
    await asyncio.sleep(0.4)
    if await page.evaluate(TIME_JS, POPUP):
        for key, value in (("hour", when.hour), ("minute", when.minute), ("second", when.second)):
            box = page.locator(f'[data-wardos-time="{key}"]')
            if await box.count():
                await box.first.fill(f"{value:02d}", timeout=5000)
    await page.evaluate(CONFIRM_JS, POPUP)
    await settle()
    return True


async def set_date(page, locator, when: datetime, *, settle, log) -> bool:
    try:
        await locator.press("Tab")
    except Exception:
        pass
    await settle()
    if await box_shows(locator, when):
        return True
    log.event("date_by_calendar", date=when.strftime("%d/%m/%Y %H:%M"))
    if await pick_in_calendar(page, locator, when, settle=settle, log=log):
        return await box_shows(locator, when)
    return False
