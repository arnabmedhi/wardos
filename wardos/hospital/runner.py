"""Play a skill step by step - no model calls - and check the page after every step."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import re
import time

from ..config import hospital_now
from . import dates, showme
from .browser import BrowserState
from .ledger import Ledger
from .pdfcapture import PdfCapture
from .pdfcheck import check_pdf
from .runlog import RunLog
from .skills import SkillLibrary, resolve_inputs
from .targets import TargetNotFound, as_pattern, is_visible, resolve

TEMPLATED_KEYS = ("value", "url", "rule", "identifier", "key")

READ_TABLE_JS = """
(onlySelectable) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden';
  const tables = Array.from(document.querySelectorAll('table')).filter(visible);
  document.querySelectorAll('[data-wardos-table]').forEach(t => t.removeAttribute('data-wardos-table'));
  let best = null, bestScore = -1;
  for (const t of tables) {
    const rows = Array.from(t.querySelectorAll('tbody tr')).filter(visible);
    const selectable = rows.filter(r => r.querySelector('input[type=checkbox]')).length;
    const score = selectable * 1000 + rows.length;
    if (score > bestScore) { best = t; bestScore = score; }
  }
  if (!best) return {headers: [], rows: []};
  best.setAttribute('data-wardos-table', '1');
  let headers = Array.from(best.querySelectorAll('thead th')).map(h => h.innerText.trim());
  const rows = [];
  Array.from(best.querySelectorAll('tbody tr')).filter(visible).forEach((tr, i) => {
    tr.setAttribute('data-wardos-row', String(i));
    const box = tr.querySelector('input[type=checkbox]');
    if (onlySelectable && !box) return;
    const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
    rows.push({index: i, cells, selectable: !!box, checked: !!(box && box.checked),
               disabled: !!(box && box.disabled)});
  });
  return {headers, rows};
}
"""


READ_ALL_TABLES_JS = """
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden';
  const tables = Array.from(document.querySelectorAll('table')).filter(visible);
  if (!tables.length) return {headers: [], rows: []};
  const heads = t => Array.from(t.querySelectorAll('thead th')).map(h => h.innerText.trim());
  const headers = heads(tables[0]);
  const seen = new Set(), rows = [];
  for (const t of tables) {
    if (heads(t).join('|') !== headers.join('|')) continue;
    for (const tr of Array.from(t.querySelectorAll('tbody tr')).filter(visible)) {
      const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
      const key = cells.join('|');
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({index: rows.length, cells, selectable: false, checked: false, disabled: false});
    }
  }
  return {headers, rows};
}
"""


class StepFailed(Exception):
    def __init__(self, step_id: str, reason: str, session_suspect: bool = False):
        super().__init__(f"step '{step_id}': {reason}")
        self.step_id = step_id
        self.reason = reason
        self.session_suspect = session_suspect


class _Stop(Exception):
    def __init__(self, status: str, scope: str):
        self.status = status
        self.scope = scope


@dataclass
class StepRecord:
    id: str
    action: str
    ok: bool
    seconds: float
    strategy: str | None = None
    drift: bool = False
    note: str | None = None


@dataclass
class RunResult:
    skill: str
    status: str
    seconds: float
    steps: list[StepRecord] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    failed_step: str | None = None
    reason: str | None = None
    session_suspect: bool = False
    drift_warnings: list[str] = field(default_factory=list)
    screenshot: str | None = None
    session_recoveries: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _Vars(dict):

    def __missing__(self, key):
        raise KeyError(f"unknown input '{key}' used in skill")


def render(value, variables: dict):
    if isinstance(value, str) and "{" in value:
        return value.format_map(_Vars(variables))
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    return value


TABLE_DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y",
                      "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                      "%d %b %Y %H:%M", "%d %b %Y")


def parse_table_date(text: str) -> datetime | None:
    text = " ".join((text or "").split())
    for fmt in TABLE_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_rule(rule: str, now: datetime | None = None) -> tuple[str, datetime | None, datetime | None]:
    base, *parts = [p.strip() for p in str(rule).strip().split(",")]
    base = base.lower()
    if base not in {"all", "new"}:
        raise ValueError(rule)
    now = now or hospital_now()
    since = until = None
    for part in parts:
        name, _, value = part.partition(":")
        name = name.strip().lower()
        if name == "days" and re.fullmatch(r"\d+", value) and int(value) > 0:
            since = datetime.combine(now.date() - timedelta(days=int(value) - 1), datetime.min.time())
        elif name == "hours" and re.fullmatch(r"\d+(?:\.\d+)?", value) and float(value) > 0:
            since = now - timedelta(hours=float(value))
        elif name in {"from", "to"}:
            moment = datetime.fromisoformat(value)
            since, until = (moment, until) if name == "from" else (since, moment)
        else:
            raise ValueError(rule)
    if since and until and since > until:
        raise ValueError(rule)
    return base, since, until


RUN_LIMIT_S = 480

SETTLE_AFTER = {"click", "click_until", "fill", "select", "check", "home", "goto", "press"}


async def _still_visible(match) -> bool:
    try:
        return await match.locator.is_visible()
    except Exception:
        return False


async def _input_value(locator) -> bool:
    try:
        return bool((await locator.input_value(timeout=2000)).strip())
    except Exception:
        return True


POSITION_COLUMN = re.compile(r"^\s*(?:s\.?\s*no\.?|sl\.?\s*no\.?|sr\.?\s*no\.?|serial(?: no\.?)?|#|action|select)\s*$", re.I)


def row_key(headers: list[str], cells: list[str]) -> str:
    return hashlib.sha256(json.dumps([headers, cells], ensure_ascii=False).encode()).hexdigest()[:24]


class SkillRunner:
    def __init__(self, state: BrowserState, *, portal, capture: PdfCapture, library: SkillLibrary,
                 log: RunLog, ledger_dir, default_timeout_s: float = 30):
        self.state = state
        self.portal = portal
        self.capture = capture
        self.library = library
        self.log = log
        self.ledger_dir = ledger_dir
        self.default_timeout_s = default_timeout_s
        self._deadline = 0.0
        self.show = showme.for_browser(state)

    async def run(self, skill_name: str, inputs: dict | None = None, *, deadline_s: float = RUN_LIMIT_S,
                  patient_key: str | None = None) -> RunResult:
        skill = self.library.load(self.portal.name, skill_name)
        started = time.monotonic()
        self._deadline = started + deadline_s
        result = RunResult(skill=skill_name, status="OK", seconds=0)
        variables = resolve_inputs(skill, inputs or {})
        ledger_patient = patient_key or str(variables.get("uhid") or variables.get("patient") or "")
        ledger = Ledger(self.ledger_dir, self.portal.name, ledger_patient) if ledger_patient else None
        self.capture.start_run(self.log.directory / "pdfs")
        self.log.event("skill_start", skill=skill_name, version=skill["version"])
        self.log.say(f"Running skill {skill_name} v{skill['version']}")
        try:
            await self._run_steps(skill, variables, result, ledger, depth=0)
        except _Stop as stop:
            result.status = stop.status
        except StepFailed as failure:
            result.status = "STEP_FAILED"
            result.failed_step = failure.step_id
            result.reason = failure.reason
            result.session_suspect = failure.session_suspect
            result.screenshot = await self._screenshot(f"failure-{failure.step_id}")
            self.log.say(f"Step '{failure.step_id}' failed: {failure.reason}"
                         + (" (session looks broken)" if failure.session_suspect else ""))
        finally:
            result.seconds = round(time.monotonic() - started, 2)
        result.outputs = {k: v for k, v in variables.items() if not k.startswith("_") and not isinstance(v, date)}
        if variables.get("_selected_keys"):
            result.outputs["selected_keys"] = list(variables["_selected_keys"])
        if ledger and skill["kind"] == "read" and result.status in {"FOUND", "NOTHING_NEW", "NOT_FOUND"}:
            if result.status == "FOUND":
                ok_pdfs = [a["sha256"] for a in result.artifacts if a.get("check", {}).get("ok")]
                ledger.remember(variables.get("_selected_keys", []), ok_pdfs, skill_name)
            ledger.record_check(skill_name, result.status, len(variables.get("_selected_keys", [])))
        self.log.event("skill_end", status=result.status, seconds=result.seconds, failed_step=result.failed_step)
        self.log.say(f"Skill {skill_name}: {result.status} in {result.seconds}s")
        return result

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _timeout(self, step: dict) -> float:
        return min(float(step.get("timeout_s", self.default_timeout_s)), self._remaining())

    async def _run_steps(self, skill: dict, variables: dict, result: RunResult, ledger, depth: int):
        if depth > 3:
            raise StepFailed("call_skill", "skills nested too deeply")
        try:
            for step in skill["steps"]:
                if self._remaining() <= 0:
                    raise StepFailed(step["id"], "run deadline reached")
                await self._run_step(step, variables, result, ledger, depth)
        except _Stop as stop:
            if stop.scope == "skill" and depth > 0:
                return
            raise

    async def _run_step(self, step: dict, variables: dict, result: RunResult, ledger, depth: int):
        record = StepRecord(id=step["id"], action=step["action"], ok=False, seconds=0)
        started = time.monotonic()
        step = {k: (render(v, variables) if k in TEMPLATED_KEYS else v) for k, v in step.items()}
        try:
            if "skip_if" in step and await self._condition(step["skip_if"], wait_s=0):
                record.ok, record.note = True, "skipped"
                return
            await self.show.step(step, variables)
            pdfs_before = len(self.capture.captured)
            match = await self._do(step, variables, result, ledger, depth)
            if match is not None:
                record.strategy, record.drift = match.strategy, match.drift
                if match.drift:
                    result.drift_warnings.append(f"{step['id']}: found via '{match.strategy}', not the first way")
            if "after" in step:
                await self._after(step["after"])
            if step["action"] in SETTLE_AFTER:
                await self.state.settle(min(25.0, max(self._remaining(), 1.0)))
            if "expect" in step:
                ok = await self._condition(step["expect"], wait_s=min(10.0, self._timeout(step)),
                                           pdfs_before=pdfs_before)
                if not ok and step["action"] == "click" and match is not None and await _still_visible(match):
                    self.log.event("click_retry", step=step["id"])
                    await self._click(match.locator, step)
                    await self.state.settle(min(25.0, max(self._remaining(), 1.0)))
                    ok = await self._condition(step["expect"], wait_s=self._timeout(step), pdfs_before=pdfs_before)
                if not ok:
                    raise StepFailed(step["id"], f"check failed: {json.dumps(step['expect'])[:200]}",
                                     session_suspect=await self._suspect())
            record.ok = True
        except _Stop:
            record.ok = True
            raise
        except StepFailed:
            raise
        except TargetNotFound as exc:
            await self._fail_or_continue(step, record, f"element not found ({exc})")
        except Exception as exc:
            await self._fail_or_continue(step, record, f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            record.seconds = round(time.monotonic() - started, 2)
            result.steps.append(record)
            self.log.event("step", **asdict(record))

    async def _fail_or_continue(self, step: dict, record: StepRecord, reason: str):
        if step.get("on_fail") == "continue":
            record.ok, record.note = True, f"continued after: {reason}"
            self.log.say(f"Step '{step['id']}' did not work ({reason}); continuing as the skill allows")
            return
        raise StepFailed(step["id"], reason, session_suspect=await self._suspect())

    async def _suspect(self) -> bool:
        try:
            return bool(await self.portal.session_suspect(await self.state.active()))
        except Exception:
            return False

    async def _do(self, step: dict, variables: dict, result: RunResult, ledger, depth: int):
        action = step["action"]
        page = await self.state.active()
        timeout_ms = int(self._timeout(step) * 1000)

        if action == "goto":
            url = step["url"]
            if url.startswith("/"):
                url = self.portal.base + url
            await page.goto(url, wait_until="domcontentloaded", timeout=max(timeout_ms, 1000))
            return None

        if action == "home":
            await self.portal.go_home(self.state, log=self.log)
            await self.state.network_quiet(500, 3)
            return None

        if action == "click":
            match = await resolve(page, step["target"], timeout_ms)
            await self._click(match.locator, step)
            return match

        if action == "click_until":
            tries = int(step.get("max_tries", 5))
            for attempt in range(tries):
                if await self._condition(step["until"], wait_s=0):
                    return None
                match = await resolve(page, step["target"], timeout_ms)
                await self.show.point(match.locator)
                await match.locator.click(timeout=timeout_ms, force=bool(step.get("force")))
                if await self._condition(step["until"], wait_s=float(step.get("wait_each_s", 2.5))):
                    return match
            raise StepFailed(step["id"], f"'until' not reached after {tries} clicks")

        if action == "fill":
            match = await resolve(page, step["target"], timeout_ms)
            value = str(step.get("value", ""))
            await self.show.point(match.locator, ring=False)
            await match.locator.fill(value, timeout=timeout_ms)
            if value and not await _input_value(match.locator):
                await match.locator.press_sequentially(value, delay=40, timeout=timeout_ms)
                if not await _input_value(match.locator):
                    raise StepFailed(step["id"], "the box did not keep the typed value")
            when = dates.parse_value(value)
            if when is not None and not await dates.set_date(
                    page, match.locator, when, settle=lambda: self.state.settle(10), log=self.log):
                raise StepFailed(step["id"], f"the date box did not take {when:%d/%m/%Y %H:%M}")
            return match

        if action == "check":
            match = await resolve(page, step["target"], timeout_ms)
            await self.show.point(match.locator)
            await match.locator.check(timeout=timeout_ms, force=bool(step.get("force")))
            return match

        if action == "select":
            match = await resolve(page, step["target"], timeout_ms)
            await self.show.point(match.locator)
            await self._select(match.locator, render(step["option"], variables), timeout_ms, page)
            return match

        if action == "press":
            await page.keyboard.press(step["key"])
            return None

        if action == "wait":
            if not await self._condition(step["condition"], wait_s=self._timeout(step)):
                raise StepFailed(step["id"], "condition not reached")
            return None

        if action == "end_if":
            if await self._condition(step["condition"], wait_s=float(step.get("wait_s", 0))):
                raise _Stop(step["status"], step.get("ends", "skill" if depth > 0 else "run"))
            return None

        if action == "call_skill":
            sub = self.library.load(self.portal.name, step["skill"])
            await self._run_steps(sub, variables, result, ledger, depth + 1)
            return None

        if action == "read_table":
            script = READ_ALL_TABLES_JS if step.get("all_tables") else READ_TABLE_JS
            table = await page.evaluate(script, bool(step.get("only_selectable", True)))
            end = time.monotonic() + min(10.0, self._remaining())
            while not table["rows"] and time.monotonic() < end:
                await asyncio.sleep(0.5)
                table = await page.evaluate(script, bool(step.get("only_selectable", True)))
            rows = [{"index": r["index"], "key": row_key(table["headers"], r["cells"]),
                     "cells": dict(zip(table["headers"], r["cells"])) if len(table["headers"]) == len(r["cells"])
                     else {f"col{i}": c for i, c in enumerate(r["cells"])},
                     "checked": r["checked"], "disabled": r["disabled"]} for r in table["rows"]]
            variables[step.get("save_as", "rows")] = rows
            variables["_table_fingerprint"] = hashlib.sha256("|".join(r["key"] for r in rows).encode()).hexdigest()
            variables["_table_headers"] = table["headers"]
            self.log.event("table_read", rows=len(rows), headers=table["headers"])
            self.log.say(f"Read {len(rows)} result row(s)")
            if not rows and step.get("on_empty"):
                raise _Stop(step["on_empty"], "run")
            return None

        if action == "select_rows":
            return await self._select_rows(step, variables, ledger, page, timeout_ms)

        if action == "select_rows_paged":
            return await self._select_rows_paged(step, variables, ledger, page, timeout_ms)

        if action == "capture_pdf":
            return await self._capture_pdf(step, variables, result, page, timeout_ms)

        raise StepFailed(step["id"], f"unsupported action {action}")

    async def _click(self, locator, step: dict):
        new_tab = step.get("new_tab")
        before = set(self.state.context.pages)
        await self.show.point(locator)
        await locator.click(timeout=int(self._timeout(step) * 1000), force=bool(step.get("force")))
        if not new_tab:
            return
        wait_s = 15.0 if new_tab == "yes" else 4.0
        end = time.monotonic() + min(wait_s, self._remaining())
        while time.monotonic() < end:
            fresh = [p for p in self.state.context.pages if p not in before and not p.is_closed()
                     and not p.url.startswith("blob:")]
            if fresh:
                page = fresh[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                self.state.page = page
                self.log.event("switched_tab")
                return
            await asyncio.sleep(0.2)
        if new_tab == "yes":
            raise StepFailed(step["id"], "expected a new tab but none opened")

    async def _select(self, locator, option: dict, timeout_ms: int, page=None):
        if page is not None and await locator.evaluate("el => el.tagName") != "SELECT":
            listed = page.get_by_role("option")
            end = time.monotonic() + timeout_ms / 1000
            while True:
                if not await listed.first.is_visible():
                    await locator.click(timeout=timeout_ms, force=True)
                try:
                    await listed.first.wait_for(timeout=2500)
                    break
                except Exception:
                    if time.monotonic() >= end:
                        raise TimeoutError("the dropdown list did not open")
                    await asyncio.sleep(0.5)
            wanted = as_pattern(str(option.get("label", option.get("value", ""))))
            if hasattr(wanted, "search"):
                choice = page.get_by_role("option", name=wanted).first
            else:
                text = re.escape(str(wanted).strip())
                choice = page.get_by_role("option", name=re.compile(rf"^\s*{text}\s*$", re.I))
                if not await choice.count():
                    choice = page.get_by_role("option", name=re.compile(rf"^\s*{text}(?:\s|\(|$)", re.I))
                choice = choice.first
            label = " ".join((await choice.inner_text(timeout=timeout_ms)).split())
            await self.show.point(choice)
            await choice.click(timeout=timeout_ms)
            end = time.monotonic() + 5
            while True:
                shown = " ".join((await locator.inner_text()).split()).casefold()
                if shown and (shown in label.casefold() or label.casefold() in shown):
                    return
                if time.monotonic() >= end:
                    raise TimeoutError("the dropdown did not keep the choice")
                await asyncio.sleep(0.3)
        if "value" in option:
            await locator.select_option(value=str(option["value"]), timeout=timeout_ms)
            return
        if "index" in option:
            await locator.select_option(index=int(option["index"]), timeout=timeout_ms)
            return
        wanted = as_pattern(str(option["label"]))
        options = await locator.evaluate(
            "el => Array.from(el.options).map(o => ({value: o.value, label: (o.label || o.textContent || '').trim()}))")
        for item in options:
            label = item["label"]
            if (wanted.search(label) if hasattr(wanted, "search") else label.strip().lower() == str(wanted).lower()):
                await locator.select_option(value=item["value"], timeout=timeout_ms)
                return
        raise ValueError(f"no option matches {option['label']!r} (options: {[o['label'] for o in options][:12]})")

    async def _select_rows(self, step: dict, variables: dict, ledger, page, timeout_ms: int):
        rows = variables.get(step.get("rows", "rows"), [])
        rule = str(step.get("rule", "all")).lower()
        candidates = [r for r in rows if not r["disabled"]]
        if rule == "new":
            seen = ledger.seen_rows() if ledger else set()
            candidates = [r for r in candidates if r["key"] not in seen]
        elif rule != "all":
            raise StepFailed(step["id"], f"unknown selection rule {rule!r}")
        if not candidates:
            variables["_selected_keys"] = []
            raise _Stop(step.get("on_none", "NOTHING_NEW"), "run")
        current = await page.evaluate(READ_TABLE_JS, True)
        current_fp = hashlib.sha256("|".join(row_key(current["headers"], r["cells"]) for r in current["rows"]).encode()).hexdigest()
        if current_fp != variables.get("_table_fingerprint"):
            raise StepFailed(step["id"], "the result table changed after it was read")
        for row in candidates:
            box = page.locator(f'table[data-wardos-table] tbody tr[data-wardos-row="{row["index"]}"] input[type=checkbox]').first
            if not await box.is_checked():
                await self.show.point(box, quick=True)
                await box.check(timeout=timeout_ms, force=bool(step.get("force")))
            if not await box.is_checked():
                raise StepFailed(step["id"], f"row {row['index']} did not stay selected")
        variables["_selected_keys"] = [r["key"] for r in candidates]
        variables["selected_count"] = len(candidates)
        self.log.say(f"Selected {len(candidates)} row(s) (rule: {rule})")
        return None

    @staticmethod
    def _row_identity(headers: list[str], cells: list[str], key_columns: list[str] | None) -> str:
        if key_columns:
            lookup = {h.strip().lower(): c for h, c in zip(headers, cells)}
            parts = [lookup.get(col.strip().lower()) for col in key_columns]
            if all(p for p in parts):
                return hashlib.sha256("|".join(" ".join(p.split()).lower() for p in parts).encode()).hexdigest()[:24]
            kept = [[h, " ".join(c.split())] for h, c in zip(headers, cells) if not POSITION_COLUMN.match(h)]
            return hashlib.sha256(json.dumps(kept, ensure_ascii=False).encode()).hexdigest()[:24]
        return row_key(headers, cells)

    @classmethod
    def _rows_from_table(cls, table: dict, page_no: int, key_columns: list[str] | None = None) -> list[dict]:
        headers = table["headers"]
        return [{"index": r["index"], "page": page_no, "key": cls._row_identity(headers, r["cells"], key_columns),
                 "cells": dict(zip(headers, r["cells"])) if len(headers) == len(r["cells"])
                 else {f"col{i}": c for i, c in enumerate(r["cells"])},
                 "checked": r["checked"], "disabled": r["disabled"]} for r in table["rows"]]

    async def _select_rows_paged(self, step: dict, variables: dict, ledger, page, timeout_ms: int):
        try:
            rule, since, until = parse_rule(step.get("rule", "all"))
        except ValueError:
            raise StepFailed(step["id"], f"unknown selection rule {step.get('rule')!r}")
        windowed = since is not None or until is not None
        date_column = step.get("date_column")
        counts = {"in_window": 0, "outside": 0, "undated": 0}
        seen = ledger.seen_rows() if (ledger and rule == "new") else set()
        pager = step.get("pager", {"css": "ul.pagination button.page-link"})
        max_pages = int(step.get("max_pages", 30))
        all_rows, chosen = [], []
        headers: list[str] = []
        page_no = 1
        while True:
            table = await page.evaluate(READ_TABLE_JS, True)
            if not headers and step.get("key_columns"):
                have = {h.strip().lower() for h in table["headers"]}
                missing = [c for c in step["key_columns"] if c.strip().lower() not in have]
                if missing:
                    self.log.event("key_columns_missing", missing=missing, headers=table["headers"])
            headers = headers or table["headers"]
            rows = self._rows_from_table(table, page_no, step.get("key_columns"))
            self._check_patient(step, variables, rows)
            all_rows += rows
            picks = [r for r in rows if not r["disabled"] and r["key"] not in seen and r["key"] not in chosen]
            if windowed:
                if date_column is None:
                    low = {h: h.strip().lower() for h in headers}
                    date_column = (next((h for h, l in low.items() if "collection" in l and "date" in l), None)
                                   or next((h for h, l in low.items() if "date" in l), None))
                    if date_column is None:
                        raise StepFailed(step["id"], "a time window needs a date column; the table has none")
                inside = []
                for r in rows:
                    when = parse_table_date(r["cells"].get(date_column, ""))
                    bucket = ("undated" if when is None else
                              "outside" if (since and when < since) or (until and when > until) else "in_window")
                    counts[bucket] += 1
                    if bucket == "in_window":
                        inside.append(r["key"])
                picks = [r for r in picks if r["key"] in inside]
            tick_started = time.monotonic()
            for row in picks:
                box = page.locator(f'table[data-wardos-table] tbody tr[data-wardos-row="{row["index"]}"] '
                                   'input[type=checkbox]').first
                if not await box.is_checked():
                    await self.show.point(box, quick=True)
                    await box.check(timeout=timeout_ms, force=bool(step.get("force")))
                if not await box.is_checked():
                    raise StepFailed(step["id"], f"page {page_no} row {row['index']} did not stay selected")
                chosen.append(row["key"])
            self.log.event("page_rows", page=page_no, rows=len(rows), selected=len(picks),
                           tick_s=round(time.monotonic() - tick_started, 2))
            turn_started = time.monotonic()
            if page_no >= max_pages or not await self._next_page(page, pager, page_no, rows, step):
                break
            self.log.event("page_turn", to_page=page_no + 1, seconds=round(time.monotonic() - turn_started, 2))
            page_no += 1
        variables[step.get("save_as", "rows")] = all_rows
        variables["_table_headers"] = headers
        variables["_selected_keys"] = chosen
        variables["selected_count"] = len(chosen)
        variables["pages_seen"] = page_no
        self.log.event("table_read", rows=len(all_rows), pages=page_no, headers=headers)
        if windowed:
            variables["window"] = {"since": since.isoformat(timespec="minutes") if since else None,
                                   "until": until.isoformat(timespec="minutes") if until else None}
            self.log.event("window", **variables["window"], date_column=date_column, **counts)
            span = (f"{since:%d %b %H:%M}" if since else "the start") + " to " + (f"{until:%d %b %H:%M}" if until else "now")
            self.log.say(f"Time window {span} on '{date_column}': {counts['in_window']} row(s) inside, "
                         f"{counts['outside']} outside" + (f", {counts['undated']} with an unreadable date" if counts["undated"] else ""))
            rule = f"{rule} within the window"
        self.log.say(f"Read {len(all_rows)} row(s) on {page_no} page(s); selected {len(chosen)} (rule: {rule})")
        if not all_rows:
            raise _Stop(step.get("on_empty", "NOT_FOUND"), "run")
        if not chosen:
            raise _Stop(step.get("on_none", "NOTHING_NEW"), "run")
        return None

    @staticmethod
    def _check_patient(step: dict, variables: dict, rows: list[dict]) -> None:
        from ..chain.normalize import same_patient
        expected, column = variables.get("patient_name"), step.get("name_column")
        if not expected or not column:
            return
        for row in rows:
            shown = {k.strip().lower(): v for k, v in row["cells"].items()}.get(column.strip().lower())
            if shown is None or same_patient({"name": expected}, {"name": shown}) is not True:
                raise _Stop("NAME_MISMATCH", "run")

    async def _next_page(self, page, pager: dict, page_no: int, rows: list[dict], step: dict) -> bool:
        target = None
        buttons = page.locator(pager["css"])
        for button in await buttons.all():
            try:
                if not await button.is_visible():
                    continue
                label = (await button.inner_text()).strip()
                aria = (await button.get_attribute("aria-label")) or ""
                disabled = (await button.get_attribute("disabled")) is not None or not await button.is_enabled()
            except Exception:
                continue
            if label == str(page_no + 1):
                target = button
                break
            if not disabled and (label in {"Next", "›", "»", ">"} or "next" in aria.lower()):
                target = target or button
        if target is None:
            return False
        shown = await page.evaluate(READ_TABLE_JS, True)
        before = {row_key(shown["headers"], r["cells"]) for r in shown["rows"]}
        numbered = (await target.inner_text()).strip() == str(page_no + 1)
        for attempt in (1, 2) if numbered else (1,):
            await self.show.point(target)
            await target.click(timeout=10000)
            end = time.monotonic() + float(step.get("page_change_s", 10))
            while time.monotonic() < end:
                await self.state.network_quiet(400, 3)
                table = await page.evaluate(READ_TABLE_JS, True)
                keys = {row_key(table["headers"], r["cells"]) for r in table["rows"]}
                if keys and keys != before:
                    if attempt > 1:
                        self.log.event("page_turn_retry", to_page=page_no + 1)
                    return True
                await asyncio.sleep(0.3)
        raise StepFailed(step["id"], f"clicked to page {page_no + 1} but the table did not change")

    async def _capture_pdf(self, step: dict, variables: dict, result: RunResult, page, timeout_ms: int):
        match = await resolve(page, step["target"], timeout_ms)
        attempts = int(step.get("max_clicks", 2))
        wait_s = float(step.get("pdf_timeout_s", 45))
        new = []
        for attempt in range(1, attempts + 1):
            before_count = len(self.capture.captured)
            before_pages = set(self.state.context.pages)
            await self.show.point(match.locator)
            await match.locator.click(timeout=timeout_ms)
            new = await self.capture.wait_for_new(before_count, timeout=min(wait_s, self._remaining()))
            if new:
                break
            opened_tab = any(p not in before_pages for p in self.state.context.pages)
            if opened_tab or attempt == attempts:
                break
            self.log.say("Print did not respond; clicking once more")
        if not new:
            raise StepFailed(step["id"], "no PDF was produced by the portal")
        identifier = step.get("identifier") or (str(variables["uhid"]) if variables.get("uhid") else None)
        any_ok = False
        for pdf in new:
            check = check_pdf(pdf.path, identifier)
            any_ok = any_ok or check.ok
            result.artifacts.append({"path": str(pdf.path), "sha256": pdf.sha256, "via": pdf.via,
                                     "size": pdf.size, "check": check.summary()})
        await self.state.close_extra_tabs()
        self.log.say(f"Captured {len(new)} PDF(s); verified: {any_ok}")
        if step.get("ends_run", True):
            raise _Stop("FOUND" if any_ok else "PDF_NOT_VERIFIED", "run")
        return match

    async def _after(self, after: dict):
        if "wait_loading" in after:
            spec = after["wait_loading"]
            loader = {"text": spec["text"]} if "text" in spec else spec["target"]
            appear_end = time.monotonic() + float(spec.get("appear_s", 2.5))
            appeared = False
            while time.monotonic() < appear_end:
                if await is_visible(await self.state.active(), loader):
                    appeared = True
                    break
                await asyncio.sleep(0.1)
            if appeared:
                gone_end = time.monotonic() + min(float(spec.get("timeout_s", 60)), self._remaining())
                while await is_visible(await self.state.active(), loader):
                    if time.monotonic() > gone_end:
                        raise StepFailed("after", "loading indicator did not finish")
                    await asyncio.sleep(0.15)
        if after.get("wait_network_quiet"):
            spec = after["wait_network_quiet"] if isinstance(after["wait_network_quiet"], dict) else {}
            await self.state.network_quiet(int(spec.get("quiet_ms", 600)),
                                           min(float(spec.get("timeout_s", 30)), self._remaining()))

    async def _condition(self, cond: dict, wait_s: float = 0, pdfs_before: int | None = None) -> bool:
        end = time.monotonic() + max(0.0, wait_s)
        while True:
            if await self._check_once(cond, pdfs_before):
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.2)

    async def _check_once(self, cond: dict, pdfs_before: int | None) -> bool:
        page = await self.state.active()
        if "any" in cond:
            return any([await self._check_once(c, pdfs_before) for c in cond["any"]])
        if "all" in cond:
            return all([await self._check_once(c, pdfs_before) for c in cond["all"]])
        if "not" in cond:
            return not await self._check_once(cond["not"], pdfs_before)
        if "visible" in cond:
            return await is_visible(page, cond["visible"])
        if "hidden" in cond:
            return not await is_visible(page, cond["hidden"])
        if "text_visible" in cond:
            return await is_visible(page, {"text": cond["text_visible"]})
        if "url_contains" in cond:
            return cond["url_contains"] in page.url
        if "pdf_captured" in cond:
            return len(self.capture.captured) > (pdfs_before or 0)
        raise ValueError(f"unknown condition {cond}")

    async def _screenshot(self, name: str) -> str | None:
        try:
            page = await self.state.active()
            path = self.log.directory / f"{name}.png"
            await page.screenshot(path=str(path), timeout=8000)
            return str(path)
        except Exception:
            return None
