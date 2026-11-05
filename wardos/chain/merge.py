"""The merge rules: new facts from one source meet the facts already in the record."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import model as m
from .normalize import IDENTITY_AUTO_KEYS, same_moment, same_value


@dataclass
class MergeResult:
    add: list[dict] = field(default_factory=list)
    update: dict[str, dict] = field(default_factory=dict)
    questions: list[dict] = field(default_factory=list)
    corroborated: int = 0
    superseded: int = 0
    disputed: int = 0


OPPOSITE = {("present", "absent"), ("absent", "present")}


def _question(admission_id: str, kind: str, group: str, facts: list[dict], text: str) -> dict:
    first = next((f for f in facts if f.get("page_id")), facts[0] if facts else {})
    return {"id": m.stable_id("q", admission_id, kind, group), "admission_id": admission_id, "kind": kind,
            "group": group, "fact_ids": [f["id"] for f in facts], "text": text, "status": "open",
            "page_id": first.get("page_id"), "source_id": first.get("source_id"),
            "answer": None, "answered_by": None, "created_at": m.now_iso()}


def _when(fact: dict) -> str:
    t = fact.get("clinical_time")
    return t.replace("T", " ") if t else "an unknown time"


def _show(fact: dict) -> str:
    v = fact.get("value") or {}
    text = v.get("text") or ""
    if fact.get("assertion") == "absent" and "no" not in text.lower().split():
        text = f"no {text}" if text else "absent"
    return f"'{text}'"


def merge(existing: list[dict], candidates: list[dict], *, admission_id: str,
          open_questions: list[dict] | None = None) -> MergeResult:
    result = MergeResult()
    pool: dict[str, dict] = {f["id"]: dict(f) for f in existing}
    questions = {q["group"]: dict(q) for q in (open_questions or []) if q.get("group")}

    def touch(fact: dict):
        pool[fact["id"]] = fact
        for i, added in enumerate(result.add):
            if added["id"] == fact["id"]:
                result.add[i] = fact
                return
        result.update[fact["id"]] = fact

    for cand in candidates:
        if cand["id"] in pool:
            continue
        same_thing = [f for f in pool.values()
                      if f["status"] in m.LIVE_STATUSES and f["type"] == cand["type"] and f["key"] == cand["key"]]

        twin = None
        for f in same_thing:
            moment = same_moment(f, cand)
            if moment == "different" or f.get("assertion") != cand.get("assertion"):
                continue
            if not same_value(f.get("value"), cand.get("value"), cand["type"], cand["key"]):
                continue
            if cand["type"] in m.SINGLE_VALUED and moment == "unknown" and (f.get("clinical_time") or cand.get("clinical_time")):
                continue
            twin = f
            break
        if twin:
            twin = dict(twin)
            for src in cand.get("support") or []:
                if src not in twin["support"]:
                    twin["support"] = twin["support"] + [src]
            if not twin.get("quote_found") and cand.get("quote_found"):
                twin["quote_found"] = True
            touch(twin)
            result.corroborated += 1
            continue

        rivals = []
        for f in ([] if cand["type"] == "identity" and cand["key"] in IDENTITY_AUTO_KEYS else same_thing):
            if same_moment(f, cand) != "same":
                continue
            if "planned" in (f.get("assertion"), cand.get("assertion")):
                continue
            opposite = (f.get("assertion"), cand.get("assertion")) in OPPOSITE
            differs = cand["type"] in m.SINGLE_VALUED and not same_value(f.get("value"), cand.get("value"), cand["type"],
                                                                    cand["key"])
            if opposite or differs:
                rivals.append(f)

        cand = dict(cand)
        if rivals:
            sample = (cand.get("value") or {}).get("sample_no")
            corrected = [f for f in rivals if cand["type"] == "lab_result" and sample
                         and (f.get("value") or {}).get("sample_no") == sample and f["status"] != m.DOCTOR_LOCKED]
            if corrected and len(corrected) == len(rivals):
                for f in corrected:
                    f = dict(f, status=m.SUPERSEDED, superseded_by=cand["id"])
                    touch(f)
                    result.superseded += 1
                cand["supersedes"] = corrected[0]["id"]
                result.add.append(cand)
                pool[cand["id"]] = cand
                continue

            locked = [f for f in rivals if f["status"] == m.DOCTOR_LOCKED]
            group = next((f["dispute_group"] for f in rivals if f.get("dispute_group")), None) \
                or m.stable_id("dg", admission_id, cand["type"], cand["key"], cand.get("clinical_time"))
            cand["status"], cand["dispute_group"] = m.DISPUTED, group
            for f in rivals:
                if f["status"] == m.ACTIVE:
                    touch(dict(f, status=m.DISPUTED, dispute_group=group))
                elif f["status"] == m.DOCTOR_LOCKED and f.get("dispute_group") != group:
                    touch(dict(f, dispute_group=group))
            members = [f for f in pool.values() if f.get("dispute_group") == group] + [cand]
            kind = m.Q_DOCTOR_CONFLICT if locked else m.Q_CONFLICT
            label = cand.get("label") or cand["key"]
            if locked:
                text = (f"You set {label} at {_when(cand)} to {_show(locked[0])}; a new page says {_show(cand)}. "
                        "Keep yours?")
            else:
                values = ", ".join(sorted({_show(f) for f in members}))
                text = f"Different values for {label} at {_when(cand)}: {values}. Which is right?"
            q = questions.get(group)
            if q is None:
                q = _question(admission_id, kind, group, members, text)
            else:
                q = dict(q, fact_ids=sorted({*q["fact_ids"], cand["id"]}), text=text)
            questions[group] = q
            result.questions = [x for x in result.questions if x["group"] != group] + [q]
            result.add.append(cand)
            pool[cand["id"]] = cand
            result.disputed += 1
            continue

        result.add.append(cand)
        pool[cand["id"]] = cand

    states = medicine_states(list(pool.values()))
    for cand in result.add:
        if cand["type"] != "med_event" or cand.get("time_precision") != "unknown" or cand.get("status") == m.RETRACTED:
            continue
        event = (cand.get("value") or {}).get("event")
        state = states.get(cand["key"])
        if not state or state["state"] == "unknown" or event not in m.MED_ON | m.MED_OFF:
            continue
        implied = "on" if event in m.MED_ON else "off"
        if implied != state["state"]:
            label = cand.get("label") or cand["key"]
            text = (f"A page says {label} was {event.replace('_', ' ')}, but has no date; the dated notes say it is "
                    f"{'running' if state['state'] == 'on' else 'stopped'}. When was this?")
            group = m.stable_id("ud", admission_id, cand["id"])
            if group not in questions:
                q = _question(admission_id, m.Q_UNCLEAR_DATE, group, [cand], text)
                questions[group] = q
                result.questions.append(q)
    return result


def _sort_moment(fact: dict) -> str:
    t = fact.get("clinical_time") or ""
    if fact.get("time_precision") == "day" and len(t) == 10:
        return t + "T12:00"
    return t


def medicine_states(facts: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for fact in facts:
        if fact.get("type") != "med_event" or fact.get("status") not in m.LIVE_STATUSES:
            continue
        entry = out.setdefault(fact["key"], {"state": "unknown", "last": None, "events": [], "discharge": False})
        entry["events"].append(fact)
        event = (fact.get("value") or {}).get("event")
        if event == "discharge_med":
            entry["discharge"] = True
            continue
        if fact.get("time_precision") == "unknown" or event not in m.MED_ON | m.MED_OFF:
            continue
        if entry["last"] is None or _sort_moment(fact) >= _sort_moment(entry["last"]):
            entry["last"] = fact
            entry["state"] = "on" if event in m.MED_ON else "off"
    for entry in out.values():
        entry["events"].sort(key=_sort_moment)
    return out
