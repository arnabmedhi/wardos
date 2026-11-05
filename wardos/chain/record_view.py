"""The full record in discharge-summary order, from the saved facts only."""
from __future__ import annotations

import re

from . import model as m
from .normalize import clean_diagnosis
from .today import _label, _nice, _ref, _time_key, _when


def _words(fact: dict) -> str:
    label, text = _nice(_label(fact)), str((fact.get("value") or {}).get("text") or "").strip()
    if not text or label.lower() in text.lower():
        return text or label
    return text if text.lower() in label.lower() else f"{label}: {text}"


def _lines(facts: list[dict], words=_words) -> list[dict]:
    seen: dict[str, dict] = {}
    for fact in sorted(facts, key=_time_key):
        text = words(fact)
        if text:
            seen[re.sub(r"\W+", " ", text).strip().lower()] = {"text": text, **_ref(fact)}
    return list(seen.values())


def _value_line(row: dict) -> dict:
    keep = ("fact_id", "source_id", "page_id", "quote", "quote_found", "time", "when", "date_doubtful", "by_doctor", "disputed")
    unit = row.get("unit") or row.get("unit_std") or ""
    return {"text": f"{row['label']} {row['value']} {unit}".strip(), "bad": bool(row.get("dir") or row.get("bad")),
            **{k: row.get(k) for k in keep}}


def _is_course(fact: dict) -> bool:
    return "course" in f"{fact.get('key')} {fact.get('label')}".lower()


def record_sections(facts: list[dict], page: dict) -> list[dict]:
    live = [f for f in facts if f.get("status") in m.LIVE_STATUSES]
    of = lambda *types: [f for f in live if f["type"] in types and f.get("assertion") != "planned"]

    named = _lines([f for f in of("diagnosis") if f.get("assertion") in (None, "present", "uncertain")],
                   lambda f: clean_diagnosis(str((f.get("value") or {}).get("text") or f.get("label") or f["key"])))
    first = {d.lower() for d in page["diagnosis"]}
    diagnosis = [x for x in named if x["text"].lower() in first] + [x for x in reversed(named) if x["text"].lower() not in first]

    vitals = [_value_line(v) for v in page["vitals"]]
    exam = [x for x in _lines(f for f in of("observation") if not _is_course(f))]

    red = [_value_line(r) for r in page["labs"] if r["dir"]]
    written = _lines(of("investigation_result"))
    pending = [{"text": p["text"], **{k: p.get(k) for k in ("fact_id", "source_id", "quote", "quote_found", "when")}}
               for p in page["plan"] if p.get("kind") == "pending"]
    in_range = [_value_line(r) for r in page["labs"] if not r["dir"]]

    running = [{"text": " ".join(x for x in (r["label"], r.get("dose")) if x), **_ref_of(r)}
               for r in page["medicines"] if r["state"] != "stopped"]
    stopped = [{"text": f"{r['label']} (stopped)", **_ref_of(r)} for r in page["medicines"] if r["state"] == "stopped"]

    events = sorted((f for f in live if f.get("assertion") != "absent"
                     and (f["type"] in ("procedure", "decision") or _is_course(f))), key=_time_key)
    course = _lines(events, lambda f: ": ".join(x for x in (_when(f.get("clinical_time")).split(",")[0], _words(f)) if x))

    plan = [{"text": p["text"], **_ref_of(p)} for p in page["plan"] if p.get("kind") != "pending"]

    def section(heading: str, lines: list[dict], short: int) -> dict:
        return {"heading": heading, "lines": lines, "short": short}
    sections = [section("Diagnosis", diagnosis, max(1, len(first))),
                section("Complaints", _lines(of("complaint")), 3),
                section("History", _lines(of("history", "allergy")), 3),
                section("Examination and vitals", vitals + exam, 4),
                section("Investigations", red + written + pending + in_range, max(3, len(red))),
                section("Treatment", running + stopped, max(3, len(running))),
                section("Course", course, 3),
                section("Plan", plan + pending, 3)]
    return [s for s in sections if s["heading"] != "Course" or s["lines"]]


def _ref_of(row: dict) -> dict:
    return {k: row.get(k) for k in ("fact_id", "source_id", "page_id", "quote", "quote_found", "time", "when", "date_doubtful",
                                    "by_doctor", "disputed")}


__all__ = ["record_sections"]
