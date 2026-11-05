"""Actions that change something (DESIGN.md 4.9): fill -> stop at the final button -> doctor approves -> one press."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import re
import time

from .targets import TargetNotFound, resolve

MAX_APPROVED_WRITES = 3

FORM_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                   && getComputedStyle(e).visibility !== 'hidden';
  const short = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const labelOf = e => {
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return short(l.innerText); }
    const wrap = e.closest('label'); if (wrap) return short(wrap.innerText);
    return short(e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name')
                 || e.getAttribute('formcontrolname') || '');
  };
  const out = [];
  for (const e of document.querySelectorAll('input, select, textarea')) {
    if (!vis(e) || ['hidden', 'password', 'submit', 'button', 'file'].includes(e.type)) continue;
    let value;
    if (e.type === 'checkbox' || e.type === 'radio') { if (!e.checked) continue; value = 'selected'; }
    else if (e.tagName === 'SELECT') value = short((e.selectedOptions[0] || {}).text || '');
    else value = short(e.value);
    if (value) out.push({label: labelOf(e), value});
  }
  return out;
}
"""

ERROR_JS = r"""
() => Array.from(document.querySelectorAll('.alert-danger, .toast-error, .swal2-html-container, mat-error, .text-danger, '
                                          + '.invalid-feedback, [role=alert], .cds--inline-notification--error, .error'))
  .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length))
  .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).join(' | ').slice(0, 300)
"""


@dataclass
class Proposal:
    button: str
    path: str
    fields: list[dict] = field(default_factory=list)
    screenshot: str = ""
    element: dict = field(default_factory=dict)

    def lines(self) -> list[str]:
        rows = [f"  {f['label'] or '(field)'}: {f['value']}" for f in self.fields] or ["  (no filled fields found)"]
        return [f"Ready to press '{self.button}' on {self.path}", "Filled in:"] + rows + [f"Screenshot: {self.screenshot}"]


def target_for(element: dict, label: str) -> dict:
    target: dict = {}
    if element.get("css"):
        target["css"] = element["css"]
        if element.get("text"):
            target["has_text"] = element["text"]
    if label:
        target["role"] = "button"
        target["name"] = "/^\\s*" + re.escape(label.strip()) + "\\s*$/i"
        target["text"] = "/^\\s*" + re.escape(label.strip()) + "\\s*$/i"
    return target


FINAL_WORDS = re.compile(r"\b(save|submit|confirm|book|sign|create|add|record|order|start|request|send|update)\b", re.I)
NEVER_WORDS = re.compile(r"\b(cancel|close|delete|remove|discard|reject|back|log\s*-?\s*out|sign\s*-?\s*out|reset|clear)\b",
                         re.I)


async def find_final_button(page) -> dict | None:
    from .explore import DESCRIBE_JS, OBSERVE_JS, kind_of
    observation = await page.evaluate(OBSERVE_JS, 180)
    buttons = [e for e in observation["elements"]
               if kind_of(e) == "button" and e.get("zone") in {"main", "dialog"} and not e.get("disabled")
               and FINAL_WORDS.search(e.get("label") or "") and not NEVER_WORDS.search(e.get("label") or "")]
    if len(buttons) != 1:
        return None
    button = buttons[0]
    return {"label": (button.get("label") or "")[:50], "path": observation["path"],
            "element": await page.evaluate(DESCRIBE_JS, button["n"]) or {}}


async def prepare(keeper, pending: dict, log) -> Proposal:
    page = await keeper.browser.active()
    shot_path = log.directory / "approval-before.png"
    await page.screenshot(path=str(shot_path), full_page=False, timeout=15000)
    fields = await page.evaluate(FORM_JS)
    proposal = Proposal(button=pending.get("label", ""), path=pending.get("path", ""), fields=fields,
                        screenshot=str(shot_path), element=pending.get("element") or {})
    log.write_json("proposal.json", asdict(proposal))
    return proposal


async def press_approved(keeper, proposal: Proposal, log, *, settle_s: float = 20) -> dict:
    page = await keeper.browser.active()
    try:
        match = await resolve(page, target_for(proposal.element, proposal.button), 10000)
    except TargetNotFound:
        return {"status": "BUTTON_GONE", "writes": [], "errors": ""}
    start = len(keeper.firewall_log)
    with keeper.firewall.allow_writes(MAX_APPROVED_WRITES):
        await match.locator.click(timeout=10000)
        end = time.monotonic() + settle_s
        await asyncio.sleep(1.0)
        while time.monotonic() < end and not await keeper.browser.network_quiet(800, 2):
            await asyncio.sleep(0.2)
    events = keeper.firewall_log[start:]
    writes = [f"{e['method']} {e['path']}" for e in events if e["decision"] == "allowed_approved"]
    blocked = [f"{e['method']} {e['path']}" for e in events if e["decision"] == "blocked"]
    page = await keeper.browser.active()
    after = log.directory / "approval-after.png"
    try:
        await page.screenshot(path=str(after), full_page=False, timeout=15000)
    except Exception:
        after = None
    errors = ""
    try:
        errors = await page.evaluate(ERROR_JS)
    except Exception:
        pass
    status = "SUBMITTED" if writes and not errors else ("SUBMITTED_WITH_MESSAGE" if writes else "NOTHING_SENT")
    outcome = {"status": status, "writes": writes, "blocked_after_window": blocked, "errors": errors,
               "screenshot_after": str(after) if after else ""}
    log.write_json("approval-outcome.json", outcome)
    return outcome
