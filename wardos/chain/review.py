"""The review rules: problems a doctor should see, each as one question."""
from __future__ import annotations

from datetime import date

from . import model as m
from .merge import medicine_states
from .normalize import clean

PENDING_AFTER_DAYS = 2


def _day(fact: dict) -> date | None:
    try:
        return date.fromisoformat((fact.get("clinical_time") or "")[:10])
    except ValueError:
        return None


def review(facts: list[dict], *, admission: dict, today: date | None = None) -> list[dict]:
    live = [f for f in facts if f["status"] in m.LIVE_STATUSES]
    questions = []

    def ask(kind: str, key: str, text: str, ids: list[str]):
        questions.append({"id": m.stable_id("q", admission["id"], kind, key, *sorted(ids)), "admission_id": admission["id"],
                          "kind": kind, "group": f"{kind}:{key}", "fact_ids": sorted(ids), "text": text,
                          "status": "open", "answer": None, "answered_by": None, "created_at": m.now_iso()})

    for key, state in medicine_states(live).items():
        if state["discharge"] and state["state"] == "off" and state["last"]:
            stop = state["last"]
            label = stop.get("label") or key
            ask("stopped_but_discharged", key,
                f"{label} was stopped on {(stop.get('clinical_time') or '')[:10]} but is on the discharge list. Correct?",
                [stop["id"]] + [e["id"] for e in state["events"] if (e.get("value") or {}).get("event") == "discharge_med"])

    results = {f["key"] for f in live if f["type"] in ("lab_result", "investigation_result")}
    today = today or date.today()
    for req in (f for f in live if f["type"] == "investigation_request"):
        asked_on = _day(req)
        if not asked_on or (today - asked_on).days < PENDING_AFTER_DAYS:
            continue
        if req["key"] in results or any(req["key"] in r or r in req["key"] for r in results if len(r) > 3):
            continue
        ask("result_pending", req["key"],
            f"{req.get('label') or req['key']} was asked for on {asked_on:%d %b}; no result is in the record yet.",
            [req["id"]])

    allergies = [f for f in live if f["type"] == "allergy" and f.get("assertion") == "present"]
    for allergy in allergies:
        words = {w for w in clean(f"{allergy['key']} {(allergy.get('value') or {}).get('text')}").split() if len(w) > 4}
        for med in (f for f in live if f["type"] == "med_event"):
            if med["key"] in words or any(w in med["key"] for w in words):
                ask("allergy_conflict", f"{allergy['key']}:{med['key']}",
                    f"Allergy to {allergy.get('label') or allergy['key']} is recorded, and "
                    f"{med.get('label') or med['key']} was given. Please check.", [allergy["id"], med["id"]])

    admitted = _day({"clinical_time": admission.get("admitted_at")})
    for f in live:
        d = _day(f)
        if d and d > today:
            ask("impossible_date", f["id"], f"{f.get('label') or f['key']} is dated {d:%d %b %Y}, in the future.",
                [f["id"]])
        elif d and admitted and f["type"] not in ("history",) and f.get("assertion") != "history" \
                and (admitted - d).days > 60 and f["type"] in ("med_event", "procedure", "vital"):
            ask("date_before_admission", f["id"],
                f"{f.get('label') or f['key']} is dated {d:%d %b %Y}, long before this admission. An old record?",
                [f["id"]])
    return questions
