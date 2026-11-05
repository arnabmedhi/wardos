"""Name and UHID picked by code, future dates, food among medicines, archive."""
from __future__ import annotations

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import classify_uhids, group_names, is_future_time, norm_uhid, not_a_medicine
from wardos.chain.record import Record

import pytest


@pytest.fixture
def rec(tmp_path):
    return Record(SQLiteDocStore(tmp_path / "record.sqlite3"), LocalFileStore(tmp_path / "files"))


def _admission(rec):
    patient = rec.create_patient(name="")
    return rec.create_admission(patient["id"], unit="Medicine Unit 1", admitted_at="2026-10-20")


def _pages(rec, adm, seen: list[dict], page_type="rounds_note"):
    docs = []
    for i, who in enumerate(seen):
        src, _ = rec.add_source(adm["id"], "text", text=f"page {i} of {len(seen)} {who}")
        page = rec.save_pages(src["id"], [{"number": 1, "text": "x"}])[0]
        rec.update_pages({page["id"]: {"patient_seen": who, "page_type": page_type}})
        docs.append((src, page))
    return docs


def _identity_fact(adm, src, page, key, text, index):
    return m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=page["id"], index=index, type="identity",
                      key=key, label=key, value={"text": text}, clinical_time=None, time_precision="unknown",
                      assertion="present", quote=text, quote_found=True)


def test_uhid_most_seen_wins_and_a_one_digit_misread_is_set_aside():
    verdict = classify_uhids({"20260888702": 3, "20260889702": 1, "20260888402": 1})
    assert verdict["winner"] == "20260888702" and verdict["ask"] == []
    assert verdict["misread"] == {"20260889702": "misread, one digit off", "20260888402": "misread, one digit off"}


def test_spaces_dashes_and_dots_do_not_make_a_second_uhid():
    assert norm_uhid("2017 0000 553") == norm_uhid("20170000-553") == norm_uhid("2017.0000.553") == "20170000553"


def test_other_hospital_numbers_are_not_conflicts():
    verdict = classify_uhids({"20260999991": 7, "600000002": 1, "ZUBAM100000001": 1, "2026099999": 2})
    assert verdict["winner"] == "20260999991" and verdict["ask"] == []
    assert "2026099999" in verdict["misread"]
    assert sorted(verdict["other"]) == ["600000002", "ZUBAM100000001"]
    verdict = classify_uhids({"20260999991": 2}, outside={"20250000001"})
    assert verdict["other"] == ["20250000001"] and verdict["winner"] == "20260999991"


def test_two_real_uhids_each_seen_twice_are_asked():
    verdict = classify_uhids({"20260100001": 2, "20260177777": 2})
    assert verdict["winner"] is None and verdict["ask"] == ["20260100001", "20260177777"]
    verdict = classify_uhids({"20260100001": 3, "20260177777": 2})
    assert verdict["winner"] is None and verdict["ask"] == ["20260100001", "20260177777"]
    verdict = classify_uhids({"20260100001": 3, "20260177777": 1})
    assert verdict["winner"] == "20260100001" and "20260177777" in verdict["misread"]
    assert classify_uhids({"20260100001": 1, "20260100009": 5}, locked="20260100001")["winner"] == "20260100001"


def test_names_that_share_a_word_are_one_patient_and_the_fullest_form_is_kept():
    groups = group_names({"Mahindra": 6, "Mr. MAHINDAR PRASAD ZADRAN": 1, "Mahindra Zadran": 2})
    assert len(groups) == 1 and groups[0]["canonical"] == "Mahindar Prasad Zadran"
    groups = group_names({"Mr. RAM LAL": 2, "Ram Lal": 1, "RAM LAL": 3})
    assert len(groups) == 1 and groups[0]["canonical"] == "Ram Lal"
    groups = group_names({"Mahindra Zadran": 2, "Mahindra": 6, "ZADRAN MAHINDRA PRASAD": 1,
                          "Mr. MAHINDAR PRASAD ZADRAN": 1})
    assert len(groups) == 1 and groups[0]["canonical"] == "Mahindar Prasad Zadran"
    groups = group_names({"Ram Lal": 3, "LAL RAM": 1, "Mr. RAM LAL KUMAR": 1, "KUMAR LAL RAM": 1})
    assert groups[0]["canonical"] == "Ram Lal Kumar"
    assert len(group_names({"Ram Lal": 3, "Sita Devi": 1})) == 2
    assert group_names({"MS CITY HOSPITAL EMERGENCY DEPT HEMATOLOGY RAM": 1}) == []


def test_resolve_identity_regroups_stored_facts_and_closes_the_old_question(rec):
    adm = _admission(rec)
    seen = ([{"name": "Mrs. Aashvi Devi", "uhid": "20260100001"}] * 3 + [{"name": "AASHVI DEVI", "uhid": "2026 0100 001"}]
            + [{"name": "Aashvi Devi", "uhid": "20260100002"}, {"name": "Aashvi", "uhid": "AB-77/12345"}])
    docs = _pages(rec, adm, seen)
    facts = [_identity_fact(adm, src, page, key, text, i)
             for i, (src, page) in enumerate(docs[:6]) for key, text in
             (("uhid", seen[i]["uhid"]), ("name", seen[i]["name"]))]
    for f in facts:
        if f["key"] == "uhid" and f["value"]["text"] in ("20260100002", "AB-77/12345"):
            f.update(status=m.DISPUTED)
        rec.store.put(m.FACTS, f)
    old = {"id": "q_old", "admission_id": adm["id"], "kind": "conflict", "group": "g_old", "status": "open",
           "fact_ids": [f["id"] for f in facts if f["key"] == "uhid"], "text": "Different values for uhid", "created_at": "1"}
    rec.store.put(m.QUESTIONS, old)

    summary = rec.resolve_identity(adm["id"])
    assert summary["uhid"] == "20260100001" and summary["name"] == "Aashvi Devi"
    patient = rec.patient_of(adm["id"])
    assert patient["uhid"] == "20260100001" and patient["name"] == "Aashvi Devi"
    assert patient["other_hospital_numbers"] == ["AB77/12345"]
    live = {f["id"]: f for f in rec.facts(adm["id"])}
    live_uhids = [f["value"]["text"] for f in live.values() if f["key"] == "uhid"]
    assert live_uhids == ["20260100001"] or sorted(set(live_uhids)) == ["20260100001"]
    assert any(f["key"] == "other_hospital_number" for f in live.values())
    misread = [f for f in rec.store.query(m.FACTS, admission_id=adm["id"]) if f["status"] == m.RETRACTED]
    assert [f["retract_reason"] for f in misread] == ["misread, one digit off"]
    assert rec.questions(adm["id"]) == []
    version = rec.admission(adm["id"])["version"]
    rec.resolve_identity(adm["id"])
    assert rec.admission(adm["id"])["version"] == version


def test_two_real_uhids_and_two_names_are_asked_once_with_the_photo_ids(rec):
    adm = _admission(rec)
    seen = [{"name": "Aashvi Devi", "uhid": "20260100001"}] * 2 + [{"name": "Sumedha Ranjika", "uhid": "20260177777"}] * 2
    docs = _pages(rec, adm, seen)
    for i, (src, page) in enumerate(docs):
        for key in ("uhid", "name"):
            rec.store.put(m.FACTS, _identity_fact(adm, src, page, key, seen[i][key], i))
    rec.resolve_identity(adm["id"])
    asked = rec.questions(adm["id"])
    assert len(asked) == 2 and all(q["page_id"] and q["source_id"] for q in asked)
    assert any("UHIDs" in q["text"] for q in asked) and any("names" in q["text"] for q in asked)
    assert rec.patient_of(adm["id"]).get("uhid") in (None, "")
    rec.resolve_identity(adm["id"])
    assert len(rec.questions(adm["id"])) == 2


def test_a_doctor_confirmed_uhid_always_wins(rec):
    adm = _admission(rec)
    docs = _pages(rec, adm, [{"uhid": "20260100001"}] * 3 + [{"uhid": "20260100009"}])
    facts = [_identity_fact(adm, src, page, "uhid", ["20260100001"] * 3 + ["20260100009"] if False else
                            ("20260100001" if i < 3 else "20260100009"), i) for i, (src, page) in enumerate(docs)]
    for f in facts:
        rec.store.put(m.FACTS, f)
    rec.confirm_fact(facts[3]["id"], doctor="dr_a")
    rec.resolve_identity(adm["id"])
    assert rec.patient_of(adm["id"])["uhid"] == "20260100009"


def test_future_clinical_date_is_marked_doubtful_when_the_fact_is_merged(rec):
    adm = _admission(rec)
    src, _ = rec.add_source(adm["id"], "text", text="note", received_at="2026-10-24T05:00:00+00:00")
    rec.save_pages(src["id"], [{"number": 1, "text": "x"}])

    def fact(index, when, text):
        return m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=index, type="vital",
                          key="pulse", label="Pulse", value={"text": text, "number": float(text)}, clinical_time=when,
                          time_precision="day", assertion="present", quote=text, quote_found=True)
    rec.apply(src["id"], [fact(0, "2026-10-26", "90"), fact(1, "2026-10-25", "80"), fact(2, "2026-10-23", "70")])
    flags = {f["value"]["text"]: bool(f.get("time_doubtful")) for f in rec.facts(adm["id"])}
    assert flags == {"90": True, "80": False, "70": False}
    assert is_future_time("2026-10-26", "2026-10-24T20:00:00+00:00") is False
    assert is_future_time("2026-10-27", "2026-10-24T05:00:00+00:00") is True


def test_food_among_medicines_is_set_aside_with_its_reason(rec):
    adm = _admission(rec)
    assert not_a_medicine("Eggs", "EGGS 2 PO TDS") and not_a_medicine("Xylitol Gum") and not not_a_medicine("Inj. Pantop")
    src, _ = rec.add_source(adm["id"], "text", text="note")
    rec.save_pages(src["id"], [{"number": 1, "text": "x"}])

    def med(index, label, text):
        return m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=index, type="med_event",
                          key=label.lower(), label=label, value={"text": text, "event": "started"},
                          clinical_time="2026-10-20", time_precision="day", assertion="present", quote=text,
                          quote_found=True)
    rec.apply(src["id"], [med(0, "Eggs", "EGGS 2 PO TDS"), med(1, "Pantoprazole", "Tab Pantop 40 mg OD")])
    assert [f["label"] for f in rec.facts(adm["id"])] == ["Pantoprazole"]
    hidden = [f for f in rec.store.query(m.FACTS, admission_id=adm["id"]) if f["status"] == m.RETRACTED]
    assert [(f["label"], f["retract_reason"]) for f in hidden] == [("Eggs", "not a medicine")]


def test_archive_takes_an_admission_off_the_ward_list_and_deletes_nothing(rec):
    from wardos.chain.api import create_app
    from fastapi.testclient import TestClient
    adm = _admission(rec)
    keep = rec.create_admission(rec.create_patient(name="RAM LAL")["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")
    api = TestClient(create_app(rec, model=object()))
    assert {w["admission"]["id"] for w in api.get("/admissions").json()} == {adm["id"], keep["id"]}
    done = rec.archive_admission(adm["id"], "dr_a")
    assert done["archived"] is True and done["archived_by"] == "dr_a" and done["archived_at"]
    assert {w["admission"]["id"] for w in api.get("/admissions").json()} == {keep["id"]}
    assert rec.admission(adm["id"])["archived"] is True
    assert any(e["type"] == "admission.archived" for e in rec.events(adm["id"]))
