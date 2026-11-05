"""Record a task shown once by a person, so it can be repeated."""
from __future__ import annotations

import re

RECORDER_JS = r"""
(origins) => {
  if (!origins.includes(location.origin) || window.__wardosRecorder) return;
  window.__wardosRecorder = true;
  const short = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const INTERACTIVE = 'button, a, input, select, textarea, option, label, [role], mat-option, mat-select, li, th, td, span, i';
  const cssPath = el => {
    const parts = [];
    for (let e = el, d = 0; e && e.nodeType === 1 && d < 4; e = e.parentElement, d++) {
      let p = e.tagName.toLowerCase();
      if (e.id) { p += '#' + e.id; parts.unshift(p); break; }
      const cls = (typeof e.className === 'string' ? e.className : '').trim().split(/\s+/).filter(c => c && !c.startsWith('ng-')).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      parts.unshift(p);
    }
    return parts.join(' > ');
  };
  const describe = (raw) => {
    const el = raw.closest(INTERACTIVE) || raw;
    const tag = el.tagName.toLowerCase();
    const info = {tag, id: el.id || '', name: el.getAttribute('name') || '', fcn: el.getAttribute('formcontrolname') || '',
      type: el.getAttribute('type') || '', role: el.getAttribute('role') || '', aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '', href: tag === 'a' ? (el.getAttribute('href') || '') : '', css: cssPath(el)};
    const cell = el.closest('td');
    if (cell) {                                   // table cell: keep the column header, never the cell text
      const table = cell.closest('table');
      const idx = Array.from(cell.parentElement.children).indexOf(cell);
      const ths = table ? Array.from(table.querySelectorAll('thead th')) : [];
      info.in_table = true; info.column = ths[idx] ? short(ths[idx].innerText) : ('col' + idx);
      info.columns = ths.map(h => short(h.innerText));          // header names only, never cell text
      info.row_has_checkbox = !!cell.parentElement.querySelector('input[type=checkbox]');
    } else if (!['input', 'textarea'].includes(tag)) {
      info.text = short(el.innerText || el.textContent);
    } else {
      info.placeholder = el.getAttribute('placeholder') || '';
      const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
      info.label = lab ? short(lab.innerText) : '';
    }
    return info;
  };
  const send = (kind, el, extra) => { try { window.__wardosRecord({kind, path: location.pathname, ...describe(el), ...(extra || {})}); } catch (e) {} };
  document.addEventListener('click', e => send('click', e.target), true);
  document.addEventListener('change', e => {
    const el = e.target, tag = el.tagName.toLowerCase();
    if (tag === 'select') send('choose', el, {option: short(el.options[el.selectedIndex] && el.options[el.selectedIndex].text)});
    else if (el.type === 'checkbox' || el.type === 'radio') send('toggle', el, {checked: el.checked});
    else send('typed', el, {value: '(not recorded)'});
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Enter') send('enter', e.target); }, true);
}
"""


def mask(path: str) -> str:
    return re.sub(r"\d{4,}", "{n}", path or "")


def summarize(event: dict) -> str:
    who = event.get("text") or event.get("label") or event.get("placeholder") or event.get("aria") or event.get("title") or ""
    if event.get("in_table"):
        who = f"table cell, column '{event.get('column')}'" + (" (row has checkbox)" if event.get("row_has_checkbox") else "")
    ident = " ".join(f"{k}={event[k]}" for k in ("id", "name", "fcn", "type", "role", "href") if event.get(k))
    extra = ""
    if event["kind"] == "choose":
        extra = f" -> option '{event.get('option')}'"
    elif event["kind"] == "toggle":
        extra = f" -> checked={event.get('checked')}"
    return f"{event['kind']:6} {mask(event.get('path', '')):22} <{event['tag']}> '{who}' {ident}{extra}  [{event.get('css', '')}]"
