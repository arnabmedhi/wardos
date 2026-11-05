"""Learn: turn a recording into a skill, prove it by replaying it, then save it."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from .skills import SkillError, validate

PASS_STATUSES = {"OK", "FOUND", "NOTHING_NEW"}

FORMAT_DOC = """
SKILL JSON
{"skill": "<name>", "portal": "<portal>", "version": 1, "kind": "read", "description": "...",
 "source": "...", "inputs": {"<name>": {"type": "string", "required": true, "default": "...", "choices": [...]}},
 "steps": [ {"id": "<unique short id>", "intent": "<what this step is for>", "action": "<action>", ...} ]}

TARGET (how to find one element; ways are tried in the written order, first unique visible match wins):
 {"css": "input[name='x']", "has_text": "Search", "role": "button", "name": "/^\\\\s*search\\\\s*$/i",
  "text": "/regex/i", "label": "...", "placeholder": "..."}   Strings like "/.../i" are regular expressions.

CONDITION: {"visible": TARGET} | {"hidden": TARGET} | {"url_contains": "/path"} |
           {"any": [COND, ...]} | {"all": [COND, ...]} | {"not": COND}

ACTIONS (only these):
 home        {}   (go to the portal's dashboard / home page, closing its pop-up notices)
 goto        {"url": "..."}
 click       {"target": T, "new_tab": "maybe"|"yes" (optional), "force": true (optional),
              "after": {"wait_loading": {"text": "/Please Wait/i", "appear_s": 3, "timeout_s": 90},
                        "wait_network_quiet": {"quiet_ms": 800, "timeout_s": 30}} (optional),
              "expect": COND (optional), "timeout_s": 20}
 click_until {"target": T, "until": COND, "max_tries": 6, "force": true}
 fill        {"target": T, "value": "{input_name}"}
 select      {"target": T, "option": {"label": "..."} | {"value": "..."}}
 check       {"target": T}
 press       {"key": "Enter"}
 wait        {"condition": COND, "timeout_s": 20}
 end_if      {"condition": COND, "status": "OK" | "NOT_FOUND" | "NOTHING_NEW"}   (stops the run when true)
 call_skill  {"skill": "<existing skill name>"}
 read_table  {"save_as": "rows", "only_selectable": false, "on_empty": "NOT_FOUND"}
              (reads every row of the main visible table into the result; use it when the task is to read
               or count something shown in a table)
 select_rows_paged {"rule": "all" | "new" | "{select}", "pager": {"css": "..."}, "key_columns": ["...", "..."],
              "save_as": "rows", "on_empty": "NOT_FOUND", "on_none": "NOTHING_NEW"}
              (reads the result table on every page, ticks each row's checkbox, moves through the pager;
               "new" skips rows already fetched before; key_columns = columns that identify a result)
 capture_pdf {"target": T, "identifier": "{uhid}", "pdf_timeout_s": 60}  (clicks e.g. Print, keeps the
              portal's own PDF, checks it, and ends the run)
 Every step may have "skip_if": COND and "on_fail": "continue" (only for optional steps).
"""

RULES = """
RULES
- The skill is replayed by a plain script with no AI, so it must be exact and general.
- Generalise: repeated ticking of rows across result pages is ONE select_rows_paged step (its pager
  handles the page-number clicks; use rule "{select}" if there is an input named 'select', else "all").
- key_columns: copy 2-3 names EXACTLY from COLUMNS OF THE RESULT TABLE that together identify one result
  (e.g. its sample/accession number + the test name). Never a tick-box/Action column, a serial-number column
  (Sl No., S.No, #) or a date alone: those do not say which result a row is.
- Targets: 2-3 ways, most reliable first: css with id/name/formcontrolname/stable classes (+ has_text for
  buttons), then role+name, then text. Never use row numbers, dates or patient-specific text. Ignore
  Angular state classes (ng-*, cdk-*, mat-focused...).
- Use {placeholders} for typed values: the recording never contains what was typed.
- If the recording starts on a page that an existing skill reaches, begin with call_skill to it. If it
  starts on the dashboard / home page, begin with {"id": "home", "action": "home"}.
- Side-menu items are often hidden until their heading is opened: open the heading with click_until
  (until the item is visible), then click the item. A click on an unlabeled arrow/icon next to a menu
  heading means "open that heading". When the recording says "unlabeled icon inside the box 'X', box
  css='...'", target that box: {"css": "<last part of the box css, e.g. div.iocn-link>", "has_text": "X",
  "text": "/^\\\\s*X\\\\s*$/i"} - never a bare tag like "a" (it matches many elements).
- If the recording ends by reading a table, end the skill with read_table (only_selectable false).
- After a search-like button add the "after" waits shown above and an "expect" for results-or-no-data.
- If the page can say there is no data (e.g. "Data not Found"), add an end_if with status NOT_FOUND.
- A click that only selected text or an empty area, and repeated clicks on the same input box, are noise.
  A button next to a box (e.g. 'Open calendar', then 'done' in the calendar) is NOT noise: date boxes often
  take the date only through their calendar, so keep those clicks in order.
- Every action recorded on the screen where the task ends is needed: never leave one out, and never make
  it optional (skip_if / on_fail) unless it closes a pop-up notice.
- A click on a non-button element whose text is a command (e.g. a div with text 'Print') means the user
  clicked that command's button: target the real button (css button + has_text + role/name).
- Reply with the skill JSON object only.
"""


def _descriptor(event: dict) -> str:
    keys = ("tag", "id", "name", "fcn", "type", "role", "aria", "href")
    bits = [f"<{event.get('tag', '?')}>"]
    who = event.get("text") or event.get("label") or event.get("placeholder") or event.get("title") or ""
    if event.get("in_table"):
        who = f"table cell, column '{event.get('column')}'" + (" (row has checkbox)" if event.get("row_has_checkbox") else "")
    if who:
        bits.append(f"'{who}'")
    bits += [f"{k}={event[k]}" for k in keys[1:] if event.get(k)]
    if event.get("css"):
        bits.append(f"css='{event['css']}'")
    if event.get("near_text"):
        bits.append(f"(unlabeled icon inside the box '{event['near_text']}', box css='{event.get('near_css', '')}')")
    return " ".join(bits)


def condense_trace(events: list[dict]) -> tuple[list[str], str]:
    rows: list[tuple[str, str, int]] = []
    start_path = events[0].get("path", "") if events else ""
    last_path = start_path
    skip_next_click_of = None
    for index, event in enumerate(events):
        kind = event.get("kind", "?")
        nxt = events[index + 1] if index + 1 < len(events) else None
        same_el = nxt is not None and all(nxt.get(k) == event.get(k) for k in ("tag", "id", "name", "css", "column"))
        if kind == "click" and same_el and nxt.get("kind") in {"typed", "toggle", "choose"}:
            continue
        if kind == "click" and event.get("in_table") and event.get("tag") == "td":
            continue
        path = event.get("path", "")
        if path and path != last_path:
            rows.append((f"(page changed to {re.sub(r'[0-9]{4,}', '{n}', path)})", f"path|{path}", 1))
            last_path = path
        extra = ""
        if kind == "typed":
            extra = f" value={event.get('value', '(not recorded)')}"
        elif kind == "choose":
            extra = f" option='{event.get('option')}'"
        elif kind == "toggle":
            extra = f" checked={event.get('checked')}"
        line = f"{kind} {_descriptor(event)}{extra}"
        identity = f"{kind}|{event.get('tag')}|{event.get('id')}|{event.get('name')}|{event.get('css')}|{event.get('column')}|{extra}"
        if rows and rows[-1][1] == identity:
            rows[-1] = (rows[-1][0], identity, rows[-1][2] + 1)
        else:
            rows.append((line, identity, 1))
    lines = [f"{i}. {line}" + (f"   x{count}" if count > 1 else "") for i, (line, _, count) in enumerate(rows, 1)]
    return lines, start_path


NO_ID = re.compile(r"\d{4,}")
ROLE_OF_TAG = {"a": "link", "button": "button"}


def _exact(text: str) -> str:
    return "/^\\s*" + re.escape(text).replace("\\ ", " ") + "\\s*$/i"


def _usable(text: str) -> bool:
    return bool(text) and not NO_ID.search(text) and len(text) <= 60


def target_of(event: dict) -> dict:
    target: dict = {}
    tag, text = event.get("tag") or "", " ".join((event.get("text") or "").split())
    aria, href, css = (event.get("aria") or "").strip(), event.get("href") or "", event.get("css") or ""
    count, index = event.get("css_count") or 0, event.get("css_index")
    if event.get("near_text") and event.get("near_css"):
        box = event["near_css"].split(">")[-1].strip()
        if not aria:
            return {"css": box, "has_text": event["near_text"], "text": _exact(event["near_text"])}
        words = " ".join(event["near_text"].split()[:2])
        target = {"css": f'{box}:has-text("{words}") {tag}[aria-label="{aria}"]'}
        if count > 1 and isinstance(index, int) and index >= 0:
            target["role"], target["name"], target["nth"] = ROLE_OF_TAG.get(tag, "button"), _exact(aria), index
        return target
    if tag == "a" and href and not NO_ID.search(href):
        target["css"] = f"a[href='{href}']"
    elif css and not NO_ID.search(css):
        target["css"] = css
        if count > 1 and _usable(text) and tag != "mat-select":
            target["has_text"] = text
        elif count > 1 and isinstance(index, int) and index >= 0:
            target["nth"] = index
    name = aria if tag in {"mat-select", "input", "textarea"} else (text or aria)
    role = event.get("role") or ROLE_OF_TAG.get(tag)
    if role and _usable(name):
        target["role"], target["name"] = role, _exact(name)
    elif _usable(text) and tag not in {"input", "textarea", "mat-select"}:
        target["text"] = _exact(text)
    if event.get("placeholder") and _usable(event["placeholder"]):
        target["placeholder"] = event["placeholder"]
    if not target:
        raise SkillError(f"a recorded <{tag}> has no stable way to be found again")
    return target


def _box(event: dict) -> str:
    return f"{event.get('tag')}|{event.get('fcn')}|{event.get('name')}|{event.get('css')}|{event.get('css_index')}"


def _pick(value: str, event: dict) -> str:
    names = value.strip("{}").split("|")
    if len(names) == 1:
        return value
    words = " ".join(str(event.get(k) or "") for k in ("fcn", "name", "placeholder", "aria", "label")).lower()
    for hints, part in ((("end", "to", "till", "until"), "to"), (("start", "from"), "from")):
        if any(h in words for h in hints):
            for name in names:
                if part in name.split("_"):
                    return "{" + name + "}"
    return "{" + names[0] + "}"


def _screen(path: str | None) -> str:
    return re.sub(r"/\d{4,}", "/{n}", path or "")


CALENDAR_PART = re.compile(r"mat-calendar|calendar-body|datepicker|flatpickr|daterangepicker", re.I)
DATE_LABEL = re.compile(r"\d{1,2}\s+[a-z]{3,9}\.?,?\s+\d{4}|[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4}"
                        r"|\b(?:previous|prev|next)\s+(?:month|year)\b", re.I)


def _calendar_pick(event: dict) -> bool:
    label = " ".join(str(event.get(k) or "") for k in ("text", "aria"))
    in_calendar = bool(CALENDAR_PART.search(event.get("css") or ""))
    return event.get("kind") == "click" and (bool(DATE_LABEL.search(label))
                                             or (in_calendar and re.fullmatch(r"\s*\d{1,2}\s*", label) is not None))


def draft_from_recording(events: list[dict], values: dict, *, start_home: bool = True) -> list[dict]:
    steps: list[dict] = [{"id": "home", "action": "home", "intent": "start from the dashboard",
                          "screen": "(dashboard)"}] if start_home else []
    kept: list[dict] = []
    for i, event in enumerate(events):
        kind = event.get("kind")
        if _calendar_pick(event):
            continue
        nxt = events[i + 1] if i + 1 < len(events) else None
        same = nxt is not None and all(nxt.get(k) == event.get(k) for k in ("tag", "id", "name", "css", "column"))
        if kind == "click" and same and nxt.get("kind") in {"typed", "toggle", "choose"}:
            continue
        if kind == "click" and event.get("in_table") and event.get("tag") == "td":
            continue
        if kind == "typed":
            value = str(event.get("value") or "")
            typed_here = [e for e in events if e.get("kind") == "typed" and _box(e) == _box(event)]
            if "{" not in value and any("{" in str(e.get("value")) for e in typed_here):
                continue
            if any(e.get("kind") == "typed" and _box(e) == _box(event) for e in kept):
                continue
        if kept and kind == "click" and kept[-1].get("kind") == "click" and _box(kept[-1]) == _box(event) \
                and kept[-1].get("text") == event.get("text"):
            continue
        kept.append(event)
    for n, event in enumerate(kept, 1):
        kind, nxt = event.get("kind"), (kept[n] if n < len(kept) else None)
        label = (event.get("text") or event.get("aria") or event.get("fcn") or event.get("option") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "_", (label if _usable(label) else event.get("tag") or kind).lower()).strip("_")[:24]
        step: dict = {"id": f"s{n}_{kind}_{slug}".rstrip("_"), "intent": f"{kind} '{label[:40]}'",
                      "screen": _screen(event.get("path"))}
        if kind == "click" and (event.get("in_table") or PAGER_LABEL.match(label or "-")):
            step.update(action="_rows", intent=f"tick / page '{label[:20]}' (row ticking)")
        elif kind == "toggle":
            step.update(action="_rows", intent="tick a result row")
        elif kind == "click":
            target = target_of(event)
            stayed = (event.get("after_path") or (nxt or {}).get("path")) == event.get("path")
            menu = (nxt is not None and nxt.get("kind") == "click" and stayed
                    and (event.get("near_text") or event.get("tag") == "a") and nxt.get("tag") == "a")
            if menu:
                step.update(action="click_until", target=target, until={"visible": target_of(nxt)}, max_tries=6)
            else:
                step.update(action="click", target=target)
        elif kind == "typed":
            value = str(event.get("value") or "")
            if not re.fullmatch(r"\{[a-z0-9_|]+\}", value):
                raise SkillError(f"a typed value in '{label}' is not one of the task inputs")
            step.update(action="fill", target=target_of(event), value=_pick(value, event))
        elif kind == "choose":
            step.update(action="select", target=target_of(event),
                        option={"label": _option(str(event.get("option") or ""), values)})
        elif kind == "read_table":
            step.update(action="read_table", only_selectable=False, save_as="rows")
        elif kind == "enter":
            step.update(action="press", key="Enter")
        else:
            continue
        after = event.get("after_path")
        step["leads_to"] = _screen(after or event.get("path"))
        if after and after != event.get("path") and not NO_ID.search(after) and step["action"] != "_rows":
            step["expect"] = {"url_contains": after}
        steps.append(step)
    return steps


def route_steps(steps: list[dict], final_screen: str) -> set[str]:
    needed = {s["id"] for s in steps if s.get("screen") == final_screen}
    screen = final_screen
    for k in range(len(steps) - 1, -1, -1):
        s = steps[k]
        if s.get("screen") != screen and s.get("leads_to") == screen and s["action"] != "home":
            needed.add(s["id"])
            if k and steps[k - 1]["action"] == "click_until" and steps[k - 1].get("screen") == s.get("screen"):
                needed.add(steps[k - 1]["id"])
            screen = s.get("screen")
    return needed


def _option(option: str, values: dict) -> str:
    for name, value in values.items():
        if name != "uhid" and value and len(str(value)) >= 3 and str(value).casefold() in option.casefold():
            return "{" + name + "}"
    if NO_ID.search(option):
        raise SkillError("a dropdown choice contains a record number; it cannot be saved in a skill")
    return option


def parse_input_spec(specs: list[str]) -> dict:
    inputs = {}
    for spec in specs or []:
        name, *parts = spec.split(":")
        entry: dict = {"type": "string"}
        for part in parts:
            if part in {"string", "date"}:
                entry["type"] = part
            elif part == "required":
                entry["required"] = True
            elif part.startswith("default="):
                entry["default"] = part[len("default="):]
            elif part.startswith("choices="):
                entry["choices"] = part[len("choices="):].split("|")
        inputs[name.strip()] = entry
    return inputs


PLAN_QUESTIONS = """
Reply with JSON only:
{"wandering": [...], "rows": null, "pdf_step": null, "no_data": null, "start_skill": null}
- wandering: ids of steps on EARLIER screens that were detours (the agent looked around before it found the
  way). Never a step on the final screen, never "home". [] if there were none.
- rows: only if result rows were ticked (steps with action _rows): {"steps": [every _rows id], "rule":
  "{select}" if there is an input named select else "all", "key_columns": [2-3 names copied EXACTLY from
  COLUMNS OF THE RESULT TABLE that together identify one result - never a tick-box/Action column, a serial
  number (Sl No., S.No, #) or a date alone], "pager_css": "css of the page-number buttons, e.g. button.page-link"}.
- pdf_step: id of the click that prints / keeps the report PDF (e.g. 'Print'), if the task keeps a PDF.
- no_data: if the screen can say there is no data: {"after": id of the search-like step,
  "text": "/No Data Found|Data not Found/i"} (the message as a regular expression).
- start_skill: if the leading steps only reach a screen that an EXISTING SKILL already reaches:
  {"skill": "<its name>", "replaces": [those leading step ids, starting with "home"]}.
"""


def build_plan_prompt(*, name: str, portal: str, description: str, inputs: dict, existing: list[dict],
                      lines: list[str], steps: list[dict], table_columns: list[str] | None, final_screen: str) -> str:
    parts = [
        "You help turn a recording of a task on a hospital website into a SKILL that a plain script replays later "
        "without any AI. The steps below were built by code EXACTLY from the recording. You do not write or change "
        "steps; you only answer the questions at the end about them.",
        f"\nSKILL NAME: {name}\nPORTAL: {portal}\nWHAT IT DOES: {description}",
        "INPUTS: " + json.dumps(inputs),
        "EXISTING SKILLS:\n" + "\n".join(f"- {s['skill']}: {s.get('description', '')}" for s in existing),
        f"FINAL SCREEN (where the task ended): {final_screen}",
    ]
    if table_columns:
        parts.append("COLUMNS OF THE RESULT TABLE: " + ", ".join(table_columns))
    parts.append("RECORDING (condensed, in order):\n" + "\n".join(lines))
    parts.append("STEPS BUILT FROM IT (id  [screen]  action  what):\n" + "\n".join(
        f"{s['id']}  [{s['screen']}]  {s['action']}  {s['intent']}" for s in steps))
    parts.append(PLAN_QUESTIONS)
    return "\n".join(parts)


def apply_plan(steps: list[dict], plan, *, final_screen: str, existing: set[str], inputs: dict,
               keeps_pdf: bool) -> list[dict]:
    if not isinstance(plan, dict):
        raise SkillError("the answer was not a JSON object")
    by_id = {s["id"]: s for s in steps}

    def step(sid):
        if sid not in by_id:
            raise SkillError(f"there is no step '{sid}'")
        return by_id[sid]

    drop, needed = set(), route_steps(steps, final_screen)
    for sid in plan.get("wandering") or []:
        if step(sid)["action"] == "home" or sid in needed:
            raise SkillError(f"step '{sid}' is on the way to the final screen; it cannot be dropped")
        drop.add(sid)
    out = [dict(s) for s in steps if s["id"] not in drop]
    ticks = [s["id"] for s in out if s["action"] == "_rows"]
    if ticks:
        rows = plan.get("rows")
        if not isinstance(rows, dict) or set(rows.get("steps") or []) != set(ticks):
            raise SkillError(f"rows.steps must list every row-ticking step: {ticks}")
        keys, pager = rows.get("key_columns") or [], str(rows.get("pager_css") or "").strip()
        if not keys or not pager:
            raise SkillError("row ticking needs key_columns and pager_css")
        first = next(k for k, s in enumerate(out) if s["action"] == "_rows")
        out = [s for s in out if s["action"] != "_rows"]
        out.insert(first, {"id": "select_rows", "action": "select_rows_paged", "screen": final_screen,
                           "intent": "tick the wanted result rows on every page",
                           "rule": "{select}" if "select" in inputs else "all", "pager": {"css": pager},
                           "key_columns": keys, "save_as": "rows", "on_empty": "NOT_FOUND", "on_none": "NOTHING_NEW"})
    pdf = plan.get("pdf_step")
    if keeps_pdf and not pdf:
        pdf = next((s["id"] for s in reversed(out) if s["action"] == "click" and s["screen"] == final_screen
                    and re.search(r"print|download|pdf", json.dumps(s.get("target")), re.I)), None)
    if pdf:
        index = next((k for k, s in enumerate(out) if s["id"] == pdf), None)
        if index is None or out[index]["action"] != "click":
            raise SkillError(f"pdf_step '{pdf}' is not a recorded click")
        click = out[index]
        out[index] = {"id": click["id"], "intent": "keep the hospital's report PDF", "screen": click["screen"],
                      "action": "capture_pdf", "target": click["target"], "pdf_timeout_s": 60,
                      **({"identifier": "{uhid}"} if "uhid" in inputs else {})}
    no_data = plan.get("no_data")
    if no_data:
        text, after = str((no_data or {}).get("text") or ""), (no_data or {}).get("after")
        index = next((k for k, s in enumerate(out) if s["id"] == after), None)
        if index is None or not text or NO_ID.search(text) or len(text) > 80:
            raise SkillError("no_data needs a recorded step id and a short message pattern")
        out.insert(index + 1, {"id": "no_data", "action": "end_if", "intent": "the screen says there is no data",
                               "condition": {"visible": {"text": text}}, "status": "NOT_FOUND", "screen": final_screen})
    start = plan.get("start_skill")
    if start:
        name, replaces = (start or {}).get("skill"), list((start or {}).get("replaces") or [])
        if name not in existing:
            raise SkillError(f"start_skill '{name}' is not an existing skill")
        if not replaces or [s["id"] for s in out[:len(replaces)]] != replaces or replaces[0] != "home":
            raise SkillError("start_skill.replaces must be the leading step ids, starting with home")
        out = [{"id": "start", "action": "call_skill", "skill": name, "intent": f"reach the screen with {name}"}] \
            + out[len(replaces):]
    for s in out:
        s.pop("screen", None)
        s.pop("leads_to", None)
    return out


def build_repair_prompt(skill: dict, failure: dict, lines: list[str], page_controls: list[str]) -> str:
    return "\n".join([
        "A skill (JSON step list replayed by a plain script) failed when it was replayed on the real website. Fix it.",
        "FAILED STEP: " + str(failure.get("failed_step")),
        "REASON: " + str(failure.get("reason")),
        "ORIGINAL RECORDING (condensed):\n" + "\n".join(lines),
        "CONTROLS VISIBLE ON THE PAGE WHERE IT FAILED (labels only):\n" + "\n".join(page_controls[:120]),
        "CURRENT SKILL:\n" + json.dumps(skill, indent=1),
        FORMAT_DOC, RULES,
        "Change only what is needed. Reply with the full corrected skill JSON only.",
    ])


def finalize(skill: dict, *, name: str, portal: str, inputs: dict, source: str) -> dict:
    skill = dict(skill)
    skill.update({"skill": name, "portal": portal, "kind": "read"})
    skill["version"] = int(skill.get("version") or 1)
    skill["inputs"] = {**inputs, **{k: v for k, v in (skill.get("inputs") or {}).items() if k not in inputs}}
    skill["source"] = source
    validate(skill)
    used = set(re.findall(r"\{(\w+)", json.dumps(skill["steps"])))
    unknown = used - set(skill["inputs"])
    if unknown:
        raise SkillError(f"skill uses unknown inputs: {sorted(unknown)}")
    return skill


def staging_dir(settings) -> Path:
    return settings.private_dir / "skills-staging"


def save_staged(settings, skill: dict) -> Path:
    folder = staging_dir(settings) / skill["portal"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{skill['skill']}.json"
    path.write_text(json.dumps(skill, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def promote(skill: dict, library_root: Path, *, verified_run: str, status: str) -> Path:
    skill = dict(skill)
    skill["verified"] = {"run": verified_run, "status": status, "date": datetime.now().isoformat(timespec="seconds")}
    path = library_root / skill["portal"] / f"{skill['skill']}.json"
    path.write_text(json.dumps(skill, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def skill_outline(skill: dict) -> list[str]:
    lines = []
    for step in skill["steps"]:
        target = step.get("target") or {}
        how = ", ".join(f"{k}={v}" for k, v in target.items() if k in ("css", "role", "name", "has_text", "text"))[:110]
        lines.append(f"  {step['id']:14} {step['action']:17} {step.get('intent', '')[:60]}" + (f"  [{how}]" if how else ""))
    return lines


async def compile_skill(models, *, events: list[dict], name: str, portal: str, description: str, inputs: dict,
                        library, table_columns: list[str] | None = None, source: str,
                        exclude: tuple[str, ...] = (), values: dict | None = None, start_home: bool = True,
                        keeps_pdf: bool = False) -> tuple[dict, list[str]]:
    lines, _ = condense_trace(events)
    columns = table_columns or next((e["columns"] for e in events if e.get("columns")), None)
    existing = [s for s in library.list(portal) if s["skill"] not in {name, *exclude}]
    steps = draft_from_recording(events, values or {}, start_home=start_home)
    final_screen = _screen(next((e.get("path") for e in reversed(events) if e.get("path")), ""))
    prompt = build_plan_prompt(name=name, portal=portal, description=description, inputs=inputs, existing=existing,
                               lines=lines, steps=steps, table_columns=columns, final_screen=final_screen)
    names = {s["skill"] for s in existing}

    def build(plan) -> dict:
        out = apply_plan(steps, plan, final_screen=final_screen, existing=names, inputs=inputs, keeps_pdf=keeps_pdf)
        skill = finalize({"description": description, "steps": out}, name=name, portal=portal, inputs=inputs,
                         source=source)
        _check_key_columns(skill, columns)
        missing = missing_final_steps(skill, events)
        if missing:
            raise SkillError("actions recorded on the final screen are missing: " + "; ".join(missing))
        return skill

    last_error = None
    for attempt in range(2):
        reply = await models.json(prompt if last_error is None else
                                  prompt + f"\nYour previous answer could not be used: {last_error}. Fix that.", timeout=120)
        try:
            return build(reply), lines
        except SkillError as exc:
            last_error = str(exc)
    if not any(s["action"] == "_rows" for s in steps):
        return build({}), lines
    raise SkillError(f"the model's answers about the recording could not be used: {last_error}")


PAGER_LABEL = re.compile(r"^(?:\d+|next|prev(?:ious)?|first|last|next page|previous page|[‹›«»<>]+)$", re.I)


def missing_final_steps(skill: dict, events: list[dict]) -> list[str]:
    final = next((e.get("path") for e in reversed(events) if e.get("path")), None)
    groups: list[tuple[str, str]] = []
    for event in events:
        kind = event.get("kind")
        if event.get("path") != final or kind not in {"click", "typed", "choose"}:
            continue
        if kind == "click" and event.get("tag") != "button":
            continue
        if _calendar_pick(event):
            continue
        label = (event.get("fcn") or event.get("aria") or event.get("text") or event.get("name") or "")
        label = label.strip().rstrip(":*").strip()
        if not label or PAGER_LABEL.match(label) or (groups and groups[-1] == (kind, label)):
            continue
        if kind == "typed" and (kind, label) in groups:
            continue
        groups.append((kind, label))
    steps = [json.dumps(s, ensure_ascii=False).casefold().replace("\\\\", "") for s in skill["steps"]]
    needed = {}
    for kind, label in groups:
        needed[label] = needed.get(label, 0) + 1
    return [f"'{label}' x{count}" if count > 1 else f"'{label}'" for label, count in needed.items()
            if sum(label.casefold() in s for s in steps) < count]


def _check_key_columns(skill: dict, columns: list[str] | None) -> None:
    from .runner import POSITION_COLUMN
    known = {c.strip().lower() for c in columns or []}
    for step in skill["steps"]:
        keys = step.get("key_columns") or []
        if step.get("action") != "select_rows_paged" or not keys:
            continue
        if known and (bad := [k for k in keys if k.strip().lower() not in known]):
            raise SkillError(f"step '{step['id']}': key_columns {bad} are not columns of the table {columns}")
        if bad := [k for k in keys if POSITION_COLUMN.match(k)]:
            raise SkillError(f"step '{step['id']}': key_columns {bad} only give a row's position")


async def verify_skill(keeper, skill: dict, replay_inputs: dict, *, settings, lines: list[str], inputs: dict,
                       library_root: Path, say=print, repair: bool = True) -> tuple[bool, str, dict, object]:
    from .skills import LayeredSkillLibrary
    keeper.library = LayeredSkillLibrary([staging_dir(settings), library_root])
    save_staged(settings, skill)
    result = None
    for round_no in (1, 2):
        result = await keeper.run_skill(skill["skill"], replay_inputs)
        say(f"Replay {round_no}: {result.status} in {result.seconds}s"
            + (f" (failed at '{result.failed_step}': {result.reason})" if result.failed_step else ""))
        if result.status in PASS_STATUSES:
            archive_existing(settings, library_root, skill)
            path = promote(skill, library_root, verified_run=f"replay {round_no}", status=result.status)
            return True, f"passed replay {round_no}; saved to {path}", skill, result
        if result.status == "NOT_FOUND":
            return False, "inconclusive: the portal shows no data for these inputs; not saved", skill, result
        if round_no == 2 or not repair:
            break
        page = await keeper.browser.active()
        controls = await page.evaluate(CONTROLS_JS)
        reply = await keeper.models.json(build_repair_prompt(skill, result.to_dict(), lines, controls), timeout=120)
        skill = finalize(reply, name=skill["skill"], portal=skill["portal"], inputs=inputs,
                         source=skill.get("source", "") + "; repaired once after a failed replay")
        save_staged(settings, skill)
        say("The model repaired the failed step; replaying again")
    return False, "replay failed; kept in staging, not saved", skill, result


def archive_existing(settings, library_root: Path, skill: dict) -> Path | None:
    current = library_root / skill["portal"] / f"{skill['skill']}.json"
    if not current.is_file():
        return None
    old = json.loads(current.read_text(encoding="utf-8"))
    folder = settings.private_dir / "skills-archive" / skill["portal"]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{skill['skill']}.v{old.get('version', 0)}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(current.read_text(encoding="utf-8"), encoding="utf-8")
    return target


CONTROLS_JS = r"""
() => Array.from(document.querySelectorAll('button, a, input, select, [role=button], [role=link], mat-select'))
  .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)).slice(0, 150)
  .map(e => {
    const tag = e.tagName.toLowerCase();
    const id = ['id', 'name', 'formcontrolname', 'type', 'href', 'aria-label'].map(k => e.getAttribute(k) ? `${k}=${e.getAttribute(k)}` : '').filter(Boolean).join(' ');
    const text = e.closest('td') ? '(table cell)' : (['input', 'textarea'].includes(tag) ? (e.getAttribute('placeholder') || '') : (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40));
    return `${tag} '${text}' ${id}`;
  })
"""


def compare(new: dict, old: dict) -> list[str]:
    a = [s["action"] for s in new["steps"]]
    b = [s["action"] for s in old["steps"]]
    lines = [f"  learned : {' -> '.join(a)}", f"  by hand : {' -> '.join(b)}"]
    lines.append("  same action order" if a == b else "  action order differs")
    return lines
