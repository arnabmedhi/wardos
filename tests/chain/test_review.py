"""The review rules."""
from __future__ import annotations

from datetime import date

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import check_iso_time, fact_key
from wardos.chain.record import Record


def setup(tmp_path):
    rec = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    patient = rec.create_patient(name="Test Patient")
    return rec, rec.create_admission(patient["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")


def add(rec, adm, tag, rows):
    src, _ = rec.add_source(adm["id"], "text", text=tag)
    docs = []
    for i, (type_, key, text, when, extra) in enumerate(rows):
        t, p = check_iso_time(when) if when else (None, "unknown")
        docs.append(m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=i, type=type_,
                               key=fact_key(type_, key), label=key, value=dict(text=text, **extra), clinical_time=t,
                               time_precision=p, assertion=extra.pop("assertion", "present") if "assertion" in extra
                               else "present", quote=text, quote_found=True))
    rec.apply(src["id"], docs)


def kinds(rec, adm):
    return sorted(q["kind"] for q in rec.questions(adm["id"]))


def test_stopped_drug_on_discharge_list(tmp_path):
    rec, adm = setup(tmp_path)
    add(rec, adm, "a", [("med_event", "ceftriaxone", "inj ceftriaxone", "2026-10-20", {"event": "started"}),
                        ("med_event", "ceftriaxone", "stop ceftriaxone", "2026-10-22", {"event": "stopped"}),
                        ("med_event", "ceftriaxone", "tab cefixime? ceftriaxone", None, {"event": "discharge_med"})])
    assert rec.review_admission(adm["id"], today=date(2026, 10, 23)) == 1
    assert "stopped_but_discharged" in kinds(rec, adm)
    assert rec.review_admission(adm["id"], today=date(2026, 10, 23)) == 0


def test_ordered_test_without_result(tmp_path):
    rec, adm = setup(tmp_path)
    add(rec, adm, "a", [("investigation_request", "CECT abdomen", "Plan CECT abdomen", "2026-10-20", {}),
                        ("investigation_request", "creatinine", "send KFT", "2026-10-20", {}),
                        ("lab_result", "creatinine", "0.9", "2026-10-21", {})])
    rec.review_admission(adm["id"], today=date(2026, 10, 23))
    (q,) = rec.questions(adm["id"])
    assert q["kind"] == "result_pending" and "CECT" in q["text"]


def test_allergy_and_future_date(tmp_path):
    rec, adm = setup(tmp_path)
    add(rec, adm, "a", [("allergy", "penicillin", "allergic to penicillin", None, {}),
                        ("med_event", "amoxicillin penicillin", "amoxicillin", "2026-10-21", {"event": "given"}),
                        ("vital", "pulse", "88/min", "2026-12-25", {})])
    rec.review_admission(adm["id"], today=date(2026, 10, 23))
    assert kinds(rec, adm) == ["allergy_conflict", "impossible_date"]
