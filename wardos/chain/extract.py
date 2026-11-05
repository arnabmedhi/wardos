"""Step 2 of an upload: page text becomes candidate facts, each with its exact quote."""
from __future__ import annotations

from datetime import date
import re
from pathlib import Path

from ..models import ModelClient, Usage
from . import model as m
from .normalize import check_iso_time, fact_key, parse_clinical_time, quote_found, split_flag

PROMPT = (Path(__file__).parent / "prompts" / "extract.md").read_text(encoding="utf-8")
EXTRACT_MAX_TOKENS = 32768
EXTRACT_HEDGE_AFTER_S = 60
PENDING = re.compile(r"\b(pending|awaited|to be sent|not yet reported)\b", re.I)


def extract_prompt(pages: list[dict], *, admitted: str | None, unit: str, meds: list[str]) -> str:
    body = "\n\n".join(f"=== PAGE {p['number']} ===\n{p['text']}" for p in pages)
    return (PROMPT.replace("<<ADMITTED>>", admitted or "not known")
            .replace("<<UNIT>>", unit or "not known")
            .replace("<<MEDS>>", ", ".join(meds) if meds else "none yet")
            + "\n\n" + body)


async def extract_facts(model: ModelClient, pages: list[dict], *, admitted: str | None, unit: str, meds: list[str],
                        model_id: str, timeout: float = 7200) -> tuple[dict, Usage]:
    raw, usage = await model.generate(extract_prompt(pages, admitted=admitted, unit=unit, meds=meds),
                                      model=model_id, max_tokens=EXTRACT_MAX_TOKENS,
                                      as_json=True, timeout=timeout, hedge_after=EXTRACT_HEDGE_AFTER_S,
                                      schema=REPLY_SCHEMA)
    return reply_shape(raw), usage


_TEXT = {"type": "STRING", "nullable": True}
REPLY_SCHEMA = {
    "type": "OBJECT", "required": ["pages", "facts"],
    "properties": {
        "pages": {"type": "ARRAY", "items": {
            "type": "OBJECT", "required": ["p"],
            "properties": {"p": {"type": "INTEGER"}, "type": _TEXT, "date": _TEXT, "patient": {
                "type": "OBJECT", "nullable": True,
                "properties": {"name": _TEXT, "uhid": _TEXT, "age": _TEXT, "sex": _TEXT}}}}},
        "facts": {"type": "ARRAY", "items": {
            "type": "OBJECT", "required": ["p", "t", "k", "v", "q"],
            "properties": {"p": {"type": "INTEGER"}, "n": {"type": "NUMBER", "nullable": True},
                           **{name: _TEXT for name in ("t", "k", "l", "v", "u", "w", "ww", "a", "e", "d", "r", "f", "s", "q")}}}},
    }}
_WRAPPERS = ("value", "data", "result", "results", "output", "response", "reply", "json", "extraction", "record")
_FACT_LISTS = ("facts", "fact", "fact_list", "extracted_facts", "items", "entries")
_PAGE_LISTS = ("pages", "page_info", "page", "pages_info")


def _is_fact(item) -> bool:
    return isinstance(item, dict) and any(k in item for k in ("t", "type", "k", "key")) \
        and any(k in item for k in ("v", "text", "q", "quote", "k", "key")) and "patient" not in item


def _as_list(value) -> list:
    return [value] if isinstance(value, dict) else value if isinstance(value, list) else []


def _collect(raw, pages: list, facts: list) -> None:
    if isinstance(raw, list):
        for item in raw:
            if _is_fact(item):
                facts.append(item)
            else:
                _collect(item, pages, facts)
    elif isinstance(raw, dict):
        found = False
        for name in _FACT_LISTS:
            if name in raw and _as_list(raw[name]):
                facts += [f for f in _as_list(raw[name]) if isinstance(f, dict)]
                found = True
        for name in _PAGE_LISTS:
            for page in _as_list(raw.get(name)):
                if isinstance(page, dict):
                    found = True
                    number = page.get("p", page.get("page"))
                    inner = [f for k in _FACT_LISTS for f in _as_list(page.get(k)) if isinstance(f, dict)]
                    facts += [{"p": number, **f} for f in inner]
                    pages.append({k: v for k, v in page.items() if k not in _FACT_LISTS})
        if "patient" in raw and not found:
            found = True
            pages.append(raw)
        if not found:
            for name in _WRAPPERS:
                if isinstance(raw.get(name), (dict, list)):
                    _collect(raw[name], pages, facts)


def _shape_words(raw) -> str:
    if isinstance(raw, dict):
        names = [k for k in raw if isinstance(k, str) and re.fullmatch(r"[A-Za-z_]{1,20}", k)][:8]
        inner = raw.get("value") if isinstance(raw.get("value"), list) else None
        if inner is not None:
            first = inner[0] if inner else None
            kind = sorted(k for k in first if re.fullmatch(r"[A-Za-z_]{1,20}", str(k)))[:8] if isinstance(first, dict) else type(first).__name__
            return f"bare list of {len(inner)}, first item {kind}"
        return f"object with {names}"
    return type(raw).__name__


def reply_shape(raw) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("facts"), list) and raw.get("pages"):
        return raw
    pages, facts = [], []
    _collect(raw, pages, facts)
    if not pages and not facts:
        raise RuntimeError(f"extraction reply has no facts list ({_shape_words(raw)})")
    if not pages:
        raise RuntimeError(f"extraction reply has no page details ({_shape_words(raw)})")
    return {"pages": pages, "facts": facts}


SHORT_YEAR = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2}\b")


def _year_misread(written, when: str, page_text: str, around: date | None) -> tuple[str | None, str]:
    text = str(written or "")
    if not text:
        day = date.fromisoformat(when[:10])
        for hit in SHORT_YEAR.finditer(page_text or ""):
            if (int(hit.group(1)), int(hit.group(2))) == (day.day, day.month) and hit.group(0)[-2:] == f"{day.year % 100:02d}":
                text = hit.group(0)
                break
    if not around or not SHORT_YEAR.search(text):
        return None, "unknown"
    return parse_clinical_time(SHORT_YEAR.sub(r"\1/\2", text, count=1), around=around)


def _clean_time(fact: dict, around: date | None, type_: str, page_text: str = "") -> tuple[str | None, str]:
    when, precision = check_iso_time(fact.get("when"))
    if when is None and fact.get("when_written"):
        when, precision = parse_clinical_time(str(fact["when_written"]), around=around)
    if when and around and type_ != "history":
        try:
            day = date.fromisoformat(when[:10])
        except ValueError:
            return None, "unknown"
        if abs((day - around).days) > 400:
            if fact.get("assertion") in ("history", "family"):
                return None, "unknown"
            return _year_misread(fact.get("when_written"), when, page_text, around)
    return when, precision


SHORT = {"p": "page", "t": "type", "k": "key", "l": "label", "v": "text", "n": "number", "u": "unit", "w": "when",
         "ww": "when_written", "a": "assertion", "e": "event", "d": "dose", "r": "route", "f": "frequency",
         "s": "sample_no", "q": "quote"}


def expand(item: dict) -> dict:
    out = {SHORT.get(k, k): v for k, v in item.items()}
    out.setdefault("assertion", "present")
    if not out.get("label"):
        out["label"] = out.get("key")
    return out


def clean_label(label: str) -> str:
    """Lab analyzers print absolute counts as "Neu#"; the "#" is print noise in a test name (the unit already says it)."""
    return " ".join(label.replace("#", " ").split()) or label


def to_candidates(raw: dict, pages: list[dict], *, admission: dict) -> list[dict]:
    around = None
    anchor = admission.get("admitted_at") or admission.get("reference_date")
    if anchor:
        try:
            around = date.fromisoformat(anchor[:10])
        except ValueError:
            around = None
    out = []
    for index, fact in enumerate(raw.get("facts") or []):
        if not isinstance(fact, dict):
            continue
        fact = expand(fact)
        try:
            page = pages[int(fact.get("page")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        type_ = fact.get("type") if fact.get("type") in m.FACT_TYPES else "observation"
        text = str(fact.get("text") or "").strip()
        key = fact_key(type_, fact.get("key"), fact.get("label") or text)
        if not key or not text:
            continue
        number = fact.get("number")
        number = float(number) if isinstance(number, (int, float)) and not isinstance(number, bool) else None
        value = {"text": text, "number": number, "unit": fact.get("unit") or None}
        if type_ == "lab_result":
            shown, flag = split_flag(text, number)
            if flag:
                value.update(text=shown, flag=flag)
        if type_ == "med_event":
            event = fact.get("event") if fact.get("event") in m.MED_EVENTS else "given"
            value.update(event=event, dose=fact.get("dose"), route=fact.get("route"),
                         frequency=fact.get("frequency"))
        if fact.get("sample_no"):
            value["sample_no"] = str(fact["sample_no"])
        when, precision = _clean_time(fact, around, type_, page["text"])
        assertion = fact.get("assertion") if fact.get("assertion") in m.ASSERTIONS else "present"
        if type_ == "lab_result" and PENDING.search(text):
            assertion = "planned"
        quote = str(fact.get("quote") or "").strip()
        doc = m.fact_doc(admission_id=admission["id"], source_id=page["source_id"], page_id=page["id"], index=index,
                         type=type_, key=key, label=(clean_label if type_ == "lab_result" else str)(str(fact.get("label") or key)), value=value, clinical_time=when,
                         time_precision=precision, assertion=assertion, quote=quote,
                         quote_found=quote_found(quote, page["text"]))
        doc["when_written"] = fact.get("when_written")
        out.append(doc)
    return out
