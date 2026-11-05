"""Offline tests for the record core."""
from __future__ import annotations

import io
import threading

import pytest

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import (check_iso_time, drug_key, fact_key, parse_clinical_time, photo_fingerprint,
                                    quote_found, same_patient)
from wardos.chain.record import Record


@pytest.fixture(params=["sqlite", "firestore"])
def rec(request, tmp_path):
    if request.param == "sqlite":
        yield Record(SQLiteDocStore(tmp_path / "record.sqlite3"), LocalFileStore(tmp_path / "files"))
        return
    import os
    import uuid
    if os.environ.get("WARDOS_TEST_FIRESTORE") != "1":
        pytest.skip("Firestore tests are off (set WARDOS_TEST_FIRESTORE=1)")
    from wardos.chain.docstore import FirestoreDocStore
    from wardos.config import load_settings
    from google.oauth2 import service_account
    settings = load_settings()
    creds = service_account.Credentials.from_service_account_file(str(settings.google_credentials))
    store = FirestoreDocStore(project="your-gcp-project", prefix=f"wardostest_{uuid.uuid4().hex[:8]}_",
                              credentials=creds)
    try:
        yield Record(store, LocalFileStore(tmp_path / "files"))
    finally:
        store.delete_all(m.ALL_COLLECTIONS)


@pytest.fixture
def adm(rec):
    patient = rec.create_patient(name="Test Patient", uhid="20260100001", sex="F", age="45")
    return rec.create_admission(patient["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")


def source(rec, adm, tag: str, kind="text"):
    src, _ = rec.add_source(adm["id"], kind, text=f"page text {tag}")
    return src


def cand(adm, src, index, type_, key, text, when=None, precision=None, assertion="present", number=None,
         unit=None, **value):
    when, precision = (check_iso_time(when) if when else (None, "unknown")) if precision is None else (when, precision)
    return m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=index, type=type_,
                      key=fact_key(type_, key), label=key, value=dict(text=text, number=number, unit=unit, **value),
                      clinical_time=when, time_precision=precision, assertion=assertion, quote=text, quote_found=True)


def test_docstore_put_get_query_and_rollback(tmp_path):
    store = SQLiteDocStore(tmp_path / "s.sqlite3")
    store.put("things", {"id": "a", "admission_id": "x", "n": 1})
    store.put("things", {"id": "b", "admission_id": "y", "n": 2, "gone": None})
    assert store.get("things", "a")["n"] == 1
    assert [d["id"] for d in store.query("things", admission_id="y")] == ["b"]
    assert [d["id"] for d in store.query("things", gone=None)] == ["a", "b"]

    def broken(tx):
        tx.put("things", {"id": "c"})
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        store.transact(broken)
    assert store.get("things", "c") is None


def test_concurrent_transactions_lose_nothing(tmp_path):
    store = SQLiteDocStore(tmp_path / "s.sqlite3")
    store.put("counter", {"id": "c", "n": 0})

    def bump():
        for _ in range(20):
            def work(tx):
                doc = tx.get("counter", "c")
                tx.put("counter", dict(doc, n=doc["n"] + 1))
            store.transact(work)
    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert store.get("counter", "c")["n"] == 80


def test_later_batch_adds_and_changes_lists_only_new(rec, adm):
    s1 = source(rec, adm, "day1")
    r1 = rec.apply(s1["id"], [cand(adm, s1, 0, "vital", "pulse", "88/min", "2026-10-20T09:00", number=88),
                              cand(adm, s1, 1, "complaint", "abdominal pain", "pain abdomen 3 days")])
    assert r1["added"] == 2 and r1["version"] == 1
    s2 = source(rec, adm, "day2")
    rec.apply(s2["id"], [cand(adm, s2, 0, "vital", "pulse", "92/min", "2026-10-21T09:00", number=92)])
    changed = rec.changes_since(adm["id"], 1)
    assert [f["value"]["text"] for f in changed] == ["92/min"]
    assert len(rec.facts(adm["id"])) == 3


def test_same_source_twice_is_a_no_op(rec, adm):
    s1 = source(rec, adm, "a")
    facts = [cand(adm, s1, 0, "vital", "pulse", "88/min", "2026-10-20T09:00", number=88)]
    rec.apply(s1["id"], facts)
    again = rec.apply(s1["id"], facts)
    assert again.get("already_merged") is True
    assert len(rec.facts(adm["id"])) == 1


def test_identical_upload_is_one_source(rec, adm):
    a, dup_a = rec.add_source(adm["id"], "photo", data=b"\xff\xd8same bytes", mime="image/jpeg")
    b, dup_b = rec.add_source(adm["id"], "photo", data=b"\xff\xd8same bytes", mime="image/jpeg")
    assert a["id"] == b["id"] and not dup_a and dup_b


def _page_image(shift: int = 0, quality: int = 90) -> bytes:
    from PIL import Image, ImageDraw
    picture = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(picture)
    for row in range(10):
        draw.line((20, 20 + row * 25 + shift, 300 - row * 12, 20 + row * 25 + shift), fill="black", width=3)
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def test_rephoto_is_marked_near_duplicate_and_facts_are_not_doubled(rec, adm):
    first, _ = rec.add_source(adm["id"], "photo", data=_page_image(0, 90), mime="image/jpeg")
    second, dup = rec.add_source(adm["id"], "photo", data=_page_image(1, 70), mime="image/jpeg")
    assert not dup and second["near_duplicate_of"] == first["id"]
    rec.apply(first["id"], [cand(adm, first, 0, "lab_result", "Hb", "9.8 g/dl", "2026-10-20", number=9.8)])
    out = rec.apply(second["id"], [cand(adm, second, 0, "lab_result", "haemoglobin", "9.8 g/dL", "2026-10-20",
                                        number=9.8)])
    assert out["corroborated"] == 1 and out["added"] == 0
    (hb,) = rec.facts(adm["id"])
    assert set(hb["support"]) == {first["id"], second["id"]}


def test_fingerprint_differs_for_different_pages():
    from wardos.chain.normalize import fingerprint_distance
    a, b = photo_fingerprint(_page_image(0)), photo_fingerprint(_page_image(0)[::-1][::-1])
    assert fingerprint_distance(a, b) == 0
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "black").save(buffer, "JPEG")
    assert fingerprint_distance(a, photo_fingerprint(buffer.getvalue())) > 6


def test_late_older_page_never_brings_a_stopped_drug_back(rec, adm):
    d1 = source(rec, adm, "day1")
    rec.apply(d1["id"], [cand(adm, d1, 0, "med_event", "Inj Ceftriaxone 1g", "Inj ceftriaxone 1g IV BD",
                              "2026-10-20", event="started")])
    d3 = source(rec, adm, "day3")
    rec.apply(d3["id"], [cand(adm, d3, 0, "med_event", "ceftriaxone", "stop ceftriaxone", "2026-10-22",
                              event="stopped")])
    late = source(rec, adm, "day2-late")
    rec.apply(late["id"], [cand(adm, late, 0, "med_event", "Inj. Ceftriaxone", "continue ceftriaxone",
                                "2026-10-21", event="continued")])
    state = rec.medicines(adm["id"])["ceftriaxone"]
    assert state["state"] == "off"
    assert [e["clinical_time"] for e in state["events"]] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    timeline = [f["clinical_time"] for f in rec.timeline(adm["id"])]
    assert timeline == sorted(timeline)


def test_undated_medicine_change_against_current_state_asks_for_the_date(rec, adm):
    d1 = source(rec, adm, "day1")
    rec.apply(d1["id"], [cand(adm, d1, 0, "med_event", "pantoprazole", "inj pan 40 OD", "2026-10-20",
                              event="started")])
    undated = source(rec, adm, "undated")
    out = rec.apply(undated["id"], [cand(adm, undated, 0, "med_event", "pantoprazole", "stop pan", event="stopped")])
    assert out["questions"] == 1
    (q,) = rec.questions(adm["id"])
    assert q["kind"] == m.Q_UNCLEAR_DATE
    assert rec.medicines(adm["id"])["pantoprazole"]["state"] == "on"


def test_two_values_same_time_are_both_kept_with_one_question(rec, adm):
    a, b, c = source(rec, adm, "a"), source(rec, adm, "b"), source(rec, adm, "c")
    rec.apply(a["id"], [cand(adm, a, 0, "lab_result", "Hb", "9.8", "2026-10-21", number=9.8)])
    out = rec.apply(b["id"], [cand(adm, b, 0, "lab_result", "hb", "8.9", "2026-10-21", number=8.9)])
    assert out["disputed"] == 1
    facts = rec.facts(adm["id"])
    assert {f["status"] for f in facts} == {m.DISPUTED} and len(facts) == 2
    rec.apply(c["id"], [cand(adm, c, 0, "lab_result", "haemoglobin", "9.1", "2026-10-21", number=9.1)])
    (q,) = rec.questions(adm["id"])
    assert q["kind"] == m.Q_CONFLICT and len(q["fact_ids"]) == 3


def test_different_days_are_not_a_conflict(rec, adm):
    a, b = source(rec, adm, "a"), source(rec, adm, "b")
    rec.apply(a["id"], [cand(adm, a, 0, "lab_result", "Hb", "9.8", "2026-10-21", number=9.8)])
    rec.apply(b["id"], [cand(adm, b, 0, "lab_result", "Hb", "10.4", "2026-10-23", number=10.4)])
    assert rec.questions(adm["id"]) == [] and {f["status"] for f in rec.facts(adm["id"])} == {m.ACTIVE}


def test_doctor_correction_is_locked_and_a_contradicting_page_asks(rec, adm):
    a = source(rec, adm, "a")
    rec.apply(a["id"], [cand(adm, a, 0, "vital", "pulse", "68/min", "2026-10-21T08:00", number=68)])
    (fact,) = rec.facts(adm["id"])
    rec.correct_fact(fact["id"], doctor="dr.a", text="86/min", number=86, unit="/min")
    (locked,) = rec.facts(adm["id"])
    assert locked["status"] == m.DOCTOR_LOCKED and locked["value"]["number"] == 86
    b = source(rec, adm, "b")
    rec.apply(b["id"], [cand(adm, b, 0, "vital", "PR", "68/min", "2026-10-21T08:00", number=68)])
    statuses = sorted(f["status"] for f in rec.facts(adm["id"]))
    assert statuses == [m.DISPUTED, m.DOCTOR_LOCKED]
    (q,) = rec.questions(adm["id"])
    assert q["kind"] == m.Q_DOCTOR_CONFLICT


def test_doctor_settles_a_dispute(rec, adm):
    a, b = source(rec, adm, "a"), source(rec, adm, "b")
    rec.apply(a["id"], [cand(adm, a, 0, "lab_result", "Hb", "9.8", "2026-10-21", number=9.8)])
    rec.apply(b["id"], [cand(adm, b, 0, "lab_result", "Hb", "8.9", "2026-10-21", number=8.9)])
    nine_eight = next(f for f in rec.facts(adm["id"]) if f["value"]["number"] == 9.8)
    rec.confirm_fact(nine_eight["id"], doctor="dr.a")
    (only,) = rec.facts(adm["id"])
    assert only["status"] == m.DOCTOR_LOCKED and only["value"]["number"] == 9.8
    assert rec.questions(adm["id"]) == []


def test_lab_correction_with_same_sample_supersedes(rec, adm):
    a, b = source(rec, adm, "a"), source(rec, adm, "b")
    rec.apply(a["id"], [cand(adm, a, 0, "lab_result", "creatinine", "1.9", "2026-10-21T10:00", number=1.9,
                             sample_no="S123")])
    rec.apply(b["id"], [cand(adm, b, 0, "lab_result", "creatinine", "1.4", "2026-10-21T10:00", number=1.4,
                             sample_no="S123")])
    (live,) = rec.facts(adm["id"])
    assert live["value"]["number"] == 1.4 and rec.questions(adm["id"]) == []


def test_opposite_assertion_same_day_is_a_conflict(rec, adm):
    a, b = source(rec, adm, "a"), source(rec, adm, "b")
    rec.apply(a["id"], [cand(adm, a, 0, "observation", "fever", "fever", "2026-10-21", assertion="present")])
    rec.apply(b["id"], [cand(adm, b, 0, "observation", "fever", "no fever", "2026-10-21", assertion="absent")])
    assert len(rec.questions(adm["id"])) == 1


def test_absent_is_kept_as_absent(rec, adm):
    a = source(rec, adm, "a")
    rec.apply(a["id"], [cand(adm, a, 0, "history", "diabetes", "No DM", assertion="absent")])
    (f,) = rec.facts(adm["id"])
    assert f["assertion"] == "absent"


def test_two_batches_for_one_admission_at_once(rec, adm):
    sources = [source(rec, adm, f"s{i}") for i in range(2)]
    batches = [[cand(adm, s, i, "vital", "pulse", f"{80 + i}/min", f"2026-10-2{i % 2}T0{i % 9}:00", number=80 + i)
                for i in range(10)] for s in sources]
    batches[1] = [dict(f, id=f["id"] + "b", key="temperature", label="temp") for f in batches[1]]
    threads = [threading.Thread(target=rec.apply, args=(s["id"], b)) for s, b in zip(sources, batches)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(rec.facts(adm["id"])) == 20
    assert rec.admission(adm["id"])["version"] == 2


def test_wrong_patient_page_is_held_with_a_question(rec, adm):
    a = source(rec, adm, "a")
    pages = rec.save_pages(a["id"], [{"number": 1, "text": "x"}, {"number": 2, "text": "y"}])
    rec.apply(a["id"], [], held_pages=[{"page_id": pages[1]["id"], "number": 2, "why": "UHID differs"}])
    (q,) = rec.questions(adm["id"])
    assert q["kind"] == m.Q_WRONG_PATIENT and q["source_id"] == a["id"]
    assert [p["held"] for p in rec.pages(adm["id"])] == [False, True]


def test_same_patient_rules():
    me = {"name": "Vir Prakash", "uhid": "20260100001"}
    assert same_patient(me, {"name": "VIRPRAKASH"}) is True
    assert same_patient(me, {"uhid": "20260100002"}) is False
    assert same_patient(me, {"name": "Sumedha Devi"}) is False
    assert same_patient(me, {"name": "(illegible)"}) is None
    assert same_patient(me, {}) is None


def test_since_last_review_is_per_doctor(rec, adm):
    a = source(rec, adm, "a")
    rec.apply(a["id"], [cand(adm, a, 0, "vital", "pulse", "88/min", "2026-10-20T09:00", number=88)])
    rec.mark_reviewed("dr.a", adm["id"])
    b = source(rec, adm, "b")
    rec.apply(b["id"], [cand(adm, b, 0, "vital", "pulse", "90/min", "2026-10-21T09:00", number=90)])
    assert [f["value"]["text"] for f in rec.since_last_review("dr.a", adm["id"])] == ["90/min"]
    assert len(rec.since_last_review("dr.b", adm["id"])) == 2


def test_times_keys_and_quotes():
    from datetime import date
    assert parse_clinical_time("21/10/26 10:30", around=date(2026, 10, 20)) == ("2026-10-21T10:30", "exact")
    assert parse_clinical_time("Date: 21.10.2026", around=date(2026, 10, 20)) == ("2026-10-21", "day")
    assert parse_clinical_time("21/10", around=date(2026, 10, 20)) == ("2026-10-21", "day")
    assert parse_clinical_time("no date here") == (None, "unknown")
    assert parse_clinical_time("21/10/2019", around=date(2026, 10, 20)) == (None, "unknown")
    assert drug_key("Inj. Ceftriaxone 1 gm") == "ceftriaxone" and drug_key("Tab Pan 40 mg") == "pan"
    assert fact_key("lab_result", "S. Creatinine") == "creatinine" and fact_key("vital", "PR") == "pulse"
    page = "Pt c/o pain abdomen x 3 days.\nNo h/o fever.  Hb - 9.8 g/dl"
    assert quote_found("No h/o fever", page) and quote_found("Hb 9.8 g/dl", page)
    assert not quote_found("vomiting since morning", page)


def test_identity_written_differently_is_not_a_conflict():
    from wardos.chain.normalize import same_value
    v = lambda t: {"text": t}
    assert same_value(v("Mr. Ram Lal"), v("RAM LAL"), "identity", "name")
    assert same_value(v("M"), v("Male"), "identity", "sex")
    assert same_value(v("63 Years"), v("63Y 9M 12D"), "identity", "age")
    assert not same_value(v("63 Years"), v("36 Years"), "identity", "age")
    assert not same_value(v("Male"), v("Female"), "identity", "sex")
    assert not same_value(v("Ram Kumar"), v("Sita Devi"), "identity", "name")


def test_pending_result_is_not_a_rival():
    from wardos.chain.merge import merge
    base = dict(type="lab_result", key="hcv", clinical_time="2026-10-13", time_precision="day")
    pending = m.fact_doc(admission_id="a", source_id="s1", page_id="p1", index=0, label="HCV",
                         value={"text": "RESULT PENDING"}, assertion="planned", quote="x", quote_found=True, **base)
    result = m.fact_doc(admission_id="a", source_id="s2", page_id="p2", index=0, label="HCV",
                        value={"text": "REACTIVE"}, assertion="present", quote="y", quote_found=True, **base)
    out = merge([pending], [result], admission_id="a", open_questions=[])
    assert out.disputed == 0 and not out.questions and len(out.add) == 1


def test_pending_lab_text_becomes_planned():
    from wardos.chain.extract import to_candidates
    page = {"id": "p1", "source_id": "s1", "text": "HCV antibody RESULT PENDING"}
    raw = {"facts": [{"p": 1, "t": "lab_result", "k": "hcv", "v": "RESULT PENDING", "q": "HCV antibody RESULT PENDING"}]}
    (cand,) = to_candidates(raw, [page], admission={"id": "a", "admitted_at": "2026-10-21"})
    assert cand["assertion"] == "planned"


def test_undated_admission_note_takes_admission_date():
    from wardos.chain.worker import _date_from_page
    cand = {"type": "observation", "assertion": "absent", "page_id": "p1", "clinical_time": None}
    _date_from_page([cand], {"p1": {"page_type": "admission_note", "page_date": None}},
                    {"admitted_at": "2026-10-21"})
    assert cand["clinical_time"] == "2026-10-21" and cand["time_from"] == "page"
    other = {"type": "observation", "assertion": "absent", "page_id": "p2", "clinical_time": None}
    _date_from_page([other], {"p2": {"page_type": "rounds_note", "page_date": None}}, {"admitted_at": "2026-10-21"})
    assert other["clinical_time"] is None


def test_uhid_misread_by_one_digit_with_same_name_is_same_patient():
    from wardos.chain.normalize import same_patient
    me = {"name": "AASHVI DEVI", "uhid": "20260100702"}
    assert same_patient(me, {"name": "Aashvi Devi", "uhid": "20260109702"}) is True
    assert same_patient(me, {"name": "Aashvi Devi", "uhid": "20260100402"}) is True
    assert same_patient(me, {"name": "Ram Kumar", "uhid": "20260109702"}) is False
    assert same_patient(me, {"name": "(illegible)", "uhid": "20260109702"}) is False
    assert same_patient(me, {"name": "Aashvi Devi", "uhid": "20260133302"}) is False


def test_add_sources_saves_each_file_once_and_keeps_near_duplicate_hint_inside_one_upload(tmp_path):
    from wardos.chain.files import LocalFileStore

    class CountingFiles(LocalFileStore):
        def __init__(self, root):
            super().__init__(root)
            self.saved = []

        def save(self, parts, name, data):
            self.saved.append(name)
            return super().save(parts, name, data)
    files = CountingFiles(tmp_path / "files")
    rec = Record(SQLiteDocStore(tmp_path / "record.sqlite3"), files)
    adm = rec.create_admission(rec.create_patient(name="T")["id"], unit="Surgery Unit 2")
    items = [dict(kind="photo", data=_page_image(0, 90), mime="image/jpeg"),
             dict(kind="photo", data=_page_image(1, 70), mime="image/jpeg"),
             dict(kind="photo", data=b"\xff\xd8 other " + bytes(range(50)), mime="image/jpeg"),
             dict(kind="photo", data=_page_image(0, 90), mime="image/jpeg")]
    out = rec.add_sources(adm["id"], items)
    assert [dup for _, dup in out] == [False, False, False, True]
    assert out[1][0]["near_duplicate_of"] == out[0][0]["id"] and out[3][0]["id"] == out[0][0]["id"]
    assert len(files.saved) == 3 and len(set(files.saved)) == 3
    assert len(rec.store.query(m.SOURCES, admission_id=adm["id"])) == 3
    assert [s["id"] for s, _ in rec.add_sources(adm["id"], items[:2])] == [out[0][0]["id"], out[1][0]["id"]]
    assert len(files.saved) == 3


def test_same_new_photo_sent_twice_at_the_same_moment_is_saved_once(tmp_path):
    import time
    from wardos.chain.files import LocalFileStore

    class SlowFiles(LocalFileStore):
        saved = 0

        def save(self, parts, name, data):
            SlowFiles.saved += 1
            time.sleep(0.3)
            return super().save(parts, name, data)
    rec = Record(SQLiteDocStore(tmp_path / "record.sqlite3"), SlowFiles(tmp_path / "files"))
    adm = rec.create_admission(rec.create_patient(name="T")["id"], unit="Surgery Unit 2")
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        rec.add_source(adm["id"], "photo", data=b"\xff\xd8 same new photo", mime="image/jpeg"))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SlowFiles.saved == 1 and sorted(dup for _, dup in results) == [False, True]
    assert len(rec.store.query(m.SOURCES, admission_id=adm["id"])) == 1
