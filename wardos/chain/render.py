"""Facts become the unit's draft discharge summary; only fields whose facts changed are written again."""
from __future__ import annotations

from collections import defaultdict
import hashlib
from pathlib import Path

from ..models import ModelClient, Usage
from . import model as m
from .record import Record
from .units import load_unit

PROMPT = (Path(__file__).parent / "prompts" / "write.md").read_text(encoding="utf-8")
DOCUMENTS = m.DOCUMENTS
FIELDS_PER_CALL = 25
WRITE_MAX_TOKENS = 32768


def _usable(facts: list[dict]) -> list[dict]:
    return [f for f in facts if f["status"] in m.LIVE_STATUSES and (f.get("quote_found") or f["created_by"] == "doctor")]


def facts_hash(facts: list[dict]) -> str:
    rows = sorted(f"{f['id']}|{f['status']}|{(f.get('value') or {}).get('text')}" for f in facts)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def fact_line(short: str, fact: dict) -> str:
    v = fact.get("value") or {}
    status = " | DISPUTED" if fact["status"] == m.DISPUTED else ""
    return (f"[{short}] {fact['type']} | {fact.get('label') or fact['key']} | {v.get('text')} | "
            f"{(fact.get('clinical_time') or 'no date').replace('T', ' ')} | {fact.get('assertion')}{status}")


def lab_table(facts: list[dict]) -> str:
    labs = [f for f in _usable(facts) if f["type"] == "lab_result" and f.get("assertion") != "planned"]
    if not labs:
        return ""
    by_test: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, str] = {}
    dates: set[str] = set()
    for f in sorted(labs, key=lambda f: f.get("clinical_time") or ""):
        day = (f.get("clinical_time") or "undated")[:10]
        dates.add(day)
        by_test[f["key"]][day].append(str((f.get("value") or {}).get("text") or ""))
        labels.setdefault(f["key"], f.get("label") or f["key"])
    columns = sorted(d for d in dates if d != "undated") + (["undated"] if "undated" in dates else [])
    head = "Test | " + " | ".join(c if c == "undated" else f"{c[8:10]}/{c[5:7]}" for c in columns)
    rows = [f"{labels[k]} | " + " | ".join(", ".join(by_test[k].get(c, [])) or "-" for c in columns)
            for k in by_test]
    return "\n".join([head] + rows)


def _field_block(field: dict) -> str:
    return f"### {field['placeholder']}\n{field['rule']}"


async def write_fields(model: ModelClient, unit: str, fields: list[dict], facts: list[dict], *, model_id: str) -> tuple[dict[str, dict], Usage | None]:
    shorts = {f"F{i + 1}": f for i, f in enumerate(facts)}
    out: dict[str, dict] = {}
    total: Usage | None = None
    for start in range(0, len(fields), FIELDS_PER_CALL):
        group = fields[start:start + FIELDS_PER_CALL]
        allowed_types = set().union(*(set(f["feeds"]) for f in group))
        lines = [fact_line(s, f) for s, f in shorts.items() if f["type"] in allowed_types]
        prompt = (PROMPT.replace("<<UNIT>>", unit).replace("<<FIELDS>>", "\n\n".join(_field_block(f) for f in group))
                  .replace("<<FACTS>>", "\n".join(lines) or "(no facts yet)"))
        raw, usage = await model.generate(prompt, model=model_id, max_tokens=WRITE_MAX_TOKENS,
                                          as_json=True, timeout=7200)
        total = usage if total is None else total.add(usage)
        written = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
        for field in group:
            item = written.get(field["placeholder"]) or written.get(field["placeholder"].strip("{}")) or {}
            text = item.get("text") if isinstance(item, dict) else item
            if not isinstance(text, str):
                text = "" if text is None else str(text)
            cited = item.get("facts") if isinstance(item, dict) else []
            cited = cited if isinstance(cited, list) else []
            ok = [shorts[c]["id"] for c in cited if c in shorts and shorts[c]["type"] in field["feeds"]]
            out[field["placeholder"]] = {"text": text.strip(), "fact_ids": ok,
                                         "dropped_ids": len(cited) - len(ok)}
    return out, total


async def update_summary(record: Record, model: ModelClient, admission_id: str, *, model_id: str) -> dict:
    admission = record.admission(admission_id)
    unit = load_unit(admission["unit"])
    facts = _usable(record.facts(admission_id))
    doc_id = m.stable_id("doc", admission_id, "discharge")
    doc = record.store.get(DOCUMENTS, doc_id) or {"id": doc_id, "admission_id": admission_id, "kind": "discharge",
                                                  "unit": admission["unit"], "fields": {}, "revision": 0}
    todo, suggestions = [], 0
    for field in unit["fields"]:
        if field["by_code"]:
            continue
        mine = [f for f in facts if f["type"] in field["feeds"]]
        h = facts_hash(mine)
        current = doc["fields"].get(field["placeholder"]) or {}
        if current.get("facts_hash") == h:
            continue
        if current.get("doctor_text") is not None:
            current["suggestion"] = True
            current["facts_hash_seen"] = h
            doc["fields"][field["placeholder"]] = current
            suggestions += 1
            continue
        if not mine:
            doc["fields"][field["placeholder"]] = {"text": "", "fact_ids": [], "facts_hash": h,
                                                   "written_in": admission["version"]}
            continue
        todo.append(field)
    written, usage = (await write_fields(model, admission["unit"], todo, facts, model_id=model_id)) if todo else ({}, None)
    for field in todo:
        mine = [f for f in facts if f["type"] in field["feeds"]]
        doc["fields"][field["placeholder"]] = dict(written.get(field["placeholder"]) or {"text": "", "fact_ids": []},
                                                    facts_hash=facts_hash(mine), written_in=admission["version"])
    doc["labs_table"] = lab_table(facts)
    doc["record_version"] = admission["version"]
    doc["revision"] = int(doc.get("revision") or 0) + 1
    doc["updated_at"] = m.now_iso()
    record.store.put(DOCUMENTS, doc)
    return {"written": len(todo), "unchanged": len(unit["fields"]) - len(todo) - suggestions,
            "suggestions": suggestions, "cost_inr": float(usage.cost_inr or 0) if usage else 0.0,
            "seconds": usage.seconds if usage else 0.0}


def edit_field(record: Record, admission_id: str, placeholder: str, text: str, *, doctor: str) -> dict:
    doc_id = m.stable_id("doc", admission_id, "discharge")
    doc = record.store.get(DOCUMENTS, doc_id)
    if not doc:
        raise KeyError("no summary yet")
    field = doc["fields"].setdefault(placeholder, {"text": "", "fact_ids": []})
    field.update(doctor_text=text, doctor=doctor, doctor_edited_at=m.now_iso(), suggestion=False)
    record.store.put(DOCUMENTS, doc)
    return field


def summary_text(record: Record, admission_id: str) -> str:
    doc = record.store.get(DOCUMENTS, m.stable_id("doc", admission_id, "discharge"))
    if not doc:
        return ""
    unit = load_unit(doc["unit"])
    parts = []
    for field in unit["fields"]:
        if field["by_code"]:
            if field["placeholder"] in ("{{labs_json}}", "{{LAB_ANCHOR}}") and doc.get("labs_table") and \
                    "INVESTIGATIONS (lab table)" not in parts:
                parts += ["INVESTIGATIONS (lab table)", doc["labs_table"], ""]
            continue
        f = doc["fields"].get(field["placeholder"]) or {}
        text = f.get("doctor_text") if f.get("doctor_text") is not None else f.get("text")
        if text:
            parts += [field["placeholder"].strip("{}").replace("_", " ").upper(), text, ""]
    return "\n".join(parts)
