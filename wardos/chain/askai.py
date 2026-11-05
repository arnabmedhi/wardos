"""Ask anything about one patient: questions the fixed ask words do not cover go to the model with only this patient's record entries."""
from __future__ import annotations

import asyncio
import re

from . import model as m
from .normalize import clean, split_flag
from .today import _sex, outside

import os

MAX_LINES = 450
ASK_MODEL = os.environ.get("WARDOS_ASK_MODEL") or None
LAB_KEEP = 4
PHOTO_WORDS = re.compile(r"\b(?:photos?|picture|pic|image|page|tasveer|foto|show\s+where|where\s+written)\b", re.I)

PROMPT = """You answer a doctor's question about ONE patient, using ONLY the record entries listed below.
Rules:
- Use only these entries and the Patient line (age and sex there are from the record; such an answer has no entry to list).
  If they do not answer the question, say plainly what is not in the record.
- Short: 1-3 sentences, plain words, values with their units and dates written like 13 Oct. Lists may use short lines.
  Answer the question first ("No, it is not getting worse: ..."), then the values that show it.
- "(low)" or "(high)" after a value means outside its normal range; "(disputed)" means a doctor has not settled it.
- For a diagnosis: the newest entries first, the main diagnosis only, one line.
- List every entry you used by its id (E1, E2 ...) in "used", most important first; do not write the ids in the answer.
- Do not advise treatment; report what the record says.
- Never state a diagnosis or condition (jaundice, anaemia, infection ...) that no entry says in words. A value is not a
  diagnosis: give the value, its date and "(high)" or "(low)", and say the record does not name the condition.
- Every entry comes from a photo or note; the screen shows the photo of each entry you name. Never say there is no
  photo - name the entries that answer the question.
Reply as JSON only: {{"answer": "...", "used": ["E3", "E12"]}}

Patient: {who}
Record entries (id | date | kind | name | value):
{lines}

Question: {question}
"""


def _date(f: dict) -> str:
    return (f.get("clinical_time") or "")[:16].replace("T", " ") or "date not written"


def _plain(f: dict) -> str:
    v = f.get("value") or {}
    text = str(v.get("text") or "").strip()
    if f.get("type") == "lab_result":
        n = v.get("number")
        text = split_flag(text, n if isinstance(n, (int, float)) else None)[0]
    return text


def _value(f: dict, sex: str | None = None) -> str:
    v = f.get("value") or {}
    text = _plain(f)
    if v.get("unit") and str(v["unit"]) not in text:
        text = f"{text} {v['unit']}"
    if f.get("type") in ("lab_result", "vital"):
        direction = outside(f, sex)[0]
        text = f"{text} ({direction})" if direction in ("low", "high") else text
    if f.get("type") == "med_event":
        text = " ".join(str(x) for x in (v.get("event"), text, v.get("dose"), v.get("route"), v.get("frequency")) if x)
    if f.get("assertion") and f["assertion"] != "present":
        text = f"{text} ({f['assertion']})"
    if f.get("status") == m.DISPUTED:
        text += " (disputed)"
    if f.get("status") == m.DOCTOR_LOCKED:
        text += " (doctor's entry)"
    return re.sub(r"\s+", " ", text)[:200]


def pick_entries(facts: list[dict], question: str) -> list[dict]:
    from .ask import _norm, test_words
    words = set(re.findall(r"[a-z0-9]{2,}", clean(f"{question} {test_words(_norm(question))}")))

    def named(f: dict) -> bool:
        return bool(words & set(re.findall(r"[a-z0-9]{2,}", clean(f"{f.get('key')} {f.get('label')}"))))
    labs: dict[str, list[dict]] = {}
    keep = []
    for f in facts:
        if f.get("type") == "lab_result" and not named(f):
            labs.setdefault(f.get("key") or f.get("label") or "", []).append(f)
        else:
            keep.append(f)
    for rows in labs.values():
        keep += sorted(rows, key=lambda f: f.get("clinical_time") or "", reverse=True)[:LAB_KEEP]
    keep.sort(key=lambda f: (f.get("clinical_time") or "9999", f.get("type") or ""))
    return keep[-MAX_LINES:]


def _row(f: dict) -> dict:
    v = f.get("value") or {}
    unit = v.get("unit") if v.get("unit") and str(v["unit"]) not in str(v.get("text") or "") else None
    return {"label": f.get("label") or f.get("key"), "value": _plain(f) or v.get("text"), "unit": unit, "fact_id": f["id"],
            "source_id": f.get("source_id"), "page_id": f.get("page_id"), "quote": f.get("quote"),
            "quote_found": f.get("quote_found"), "when": _date(f), "by_doctor": f.get("status") == m.DOCTOR_LOCKED}


async def answer_anything(record, model, model_id: str, patient_block: dict, question: str) -> dict:
    every = await asyncio.to_thread(record.facts, patient_block["id"])
    facts = [f for f in every if f.get("type") != "identity"]
    if not facts:
        return {"kind": "message", "title": "Answer", "patient": patient_block,
                "text": f"Nothing is in {patient_block['bed_label']}'s record yet. Add photos first."}
    entries = pick_entries(facts, question)
    patient = await asyncio.to_thread(record.patient_of, patient_block["id"]) or {}
    sex = _sex(patient, every)
    age = patient.get("age") or next(((f.get("value") or {}).get("text") for f in every
                                      if f.get("type") == "identity" and f.get("key") == "age"), None)
    who = ", ".join(str(x) for x in (patient_block["label"], f"age {age}" if age else None,
                                     {"M": "male", "F": "female"}.get(sex, sex)) if x)
    lines = "\n".join(f"E{i} | {_date(f)} | {f.get('type')} | {f.get('label') or f.get('key')} | {_value(f, sex)}"
                      for i, f in enumerate(entries, 1))
    raw, usage = await model.generate(PROMPT.format(who=who, question=question.strip(), lines=lines),
                                      model=ASK_MODEL or model_id, max_tokens=1500, as_json=True, timeout=7200,
                                      hedge_after=20)
    raw = raw if isinstance(raw, dict) else {}
    used = []
    for tag in raw.get("used") or []:
        hit = re.fullmatch(r"E?(\d+)", str(tag).strip(), flags=re.I)
        if hit and 1 <= int(hit.group(1)) <= len(entries) and entries[int(hit.group(1)) - 1] not in used:
            used.append(entries[int(hit.group(1)) - 1])
    text = re.sub(r"\s*\((?:E\d+[,;\s]*(?:and\s+)?)+\)", "", str(raw.get("answer") or ""))
    text = re.sub(r"\bE\d+\b,?\s*", "", text).strip() or "I could not get an answer. Try asking in other words."
    return {"kind": "answer", "title": "Answer", "patient": patient_block, "text": text,
            "sources": [_row(f) for f in used[:8]], "show_photo": bool(PHOTO_WORDS.search(question)),
            "cost_inr": round(float(usage.cost_inr or 0), 3) if usage else None,
            "seconds": round(float(usage.seconds or 0), 1) if usage else None, "entries_sent": len(entries)}
