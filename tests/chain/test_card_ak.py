"""A held page added on 'It is this patient', attendant names, and flag letters in a value."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from wardos.chain import model as m
from wardos.chain.api import create_app
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.extract import to_candidates
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import split_flag
from wardos.chain.record import Record
from wardos.chain.settings import ChainSettings
from wardos.chain.today import _show, outside, question_rows
from wardos.models import Usage

from .test_api import InlineQueue

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6, store="sqlite", files="local")


class NamedPageModel:

    async def generate(self, prompt, **kwargs):
        pages = re.split(r"^=== PAGE (\d+) ===$", prompt, flags=re.M)[1:]
        facts, info = [], []
        for number, text in zip(pages[::2], pages[1::2]):
            day = re.search(r"Date (\d+)/10", text)
            when = f"2026-10-{int(day.group(1)):02d}" if day else None
            name = re.search(r"^Name: (.+)$", text, flags=re.M)
            info.append({"p": int(number), "type": "rounds_note", "date": when,
                         "patient": {"name": name.group(1)} if name else {}})
            for pr in re.findall(r"PR (\d+)/min", text):
                facts.append({"p": int(number), "t": "vital", "k": "pulse", "v": f"{pr}/min", "n": int(pr), "w": when,
                              "q": f"PR {pr}/min"})
            for flag, urea in re.findall(r"Urea (H |L )?(\d+)", text):
                facts.append({"p": int(number), "t": "lab_result", "k": "urea", "v": f"{flag}{urea}", "n": int(urea),
                              "w": when, "q": f"Urea {flag}{urea}"})
        return {"pages": info, "facts": facts}, Usage("fake", cost_inr=0.0)


def client(tmp_path):
    queue = InlineQueue()
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")),
                     model=NamedPageModel(), settings=SETTINGS, queue=queue)
    queue.app = app
    return TestClient(app)


def world(tmp_path, *, extra_note="Name: Ramesh Chandra Joshi\nDate 21/10\nPR 99/min\nUrea H 426"):
    api = client(tmp_path)
    patient = api.post("/patients", json={"name": "Test Patient"}).json()
    aid = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2", "bed": "7",
                                        "admitted_at": "2026-10-20"}).json()["id"]
    api.post(f"/admissions/{aid}/notes", json={"text": "Name: Test Patient\nDate 20/10\nPR 88/min"})
    api.post(f"/admissions/{aid}/notes", json={"text": extra_note})
    return api, api.app.state.record, aid


def held_question(r, aid):
    (q,) = [q for q in r.questions(aid) if q["kind"] == m.Q_WRONG_PATIENT]
    return q


def test_held_page_values_wait_apart_and_are_not_in_the_record(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    assert [f["value"]["text"] for f in r.facts(aid)] == ["88/min"]
    waiting = r.store.get(m.HELD, m.stable_id("held", q["page_id"]))
    assert waiting["status"] == "set_aside" and len(waiting["candidates"]) == 2
    row = api.get(f"/admissions/{aid}/today").json()["questions"][0]
    assert row["note"] == "" and [o["value"] for o in row["options"]] == ["Hold it", "It is this patient"]


def test_it_is_this_patient_adds_every_value_with_its_page_and_closes_the_question(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    out = api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"}).json()
    assert out["added"] == 2 and not out["values_not_added"]
    facts = {f["key"]: f for f in r.facts(aid)}
    assert {"pulse", "urea"} <= set(facts)
    held_page = r.store.get(m.PAGES, q["page_id"])
    assert held_page["held"] is False
    for key in ("urea",):
        assert facts[key]["page_id"] == q["page_id"] and facts[key]["source_id"] == q["source_id"]
    assert sorted(f["value"]["text"] for f in r.facts(aid, live_only=True) if f["page_id"] == q["page_id"]) == \
        ["426", "99/min"]
    assert r.store.get(m.QUESTIONS, q["id"])["status"] == "answered"
    assert not [x for x in api.get(f"/admissions/{aid}/today").json()["questions"] if x["id"] == q["id"]]
    assert r.store.get(m.HELD, m.stable_id("held", q["page_id"]))["candidates"] == []


def test_pressing_it_twice_or_both_buttons_adds_nothing_twice(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"})
    version, count = r.admission(aid)["version"], len(r.facts(aid, live_only=False))
    again = api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"}).json()
    other = api.post(f"/questions/{q['id']}/answer", json={"answer": "Hold it"}).json()
    assert again["added"] == 0 and other["added"] == 0 and again.get("already_answered")
    assert r.admission(aid)["version"] == version and len(r.facts(aid, live_only=False)) == count
    assert r.store.get(m.PAGES, q["page_id"])["held"] is False


def test_a_reload_in_the_middle_changes_nothing_until_the_answer_is_saved(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    before = (r.admission(aid)["version"], len(r.facts(aid, live_only=False)))
    api.get(f"/admissions/{aid}/today")
    assert held_question(r, aid)["id"] == q["id"] and r.store.get(m.QUESTIONS, q["id"])["status"] == "open"
    assert (r.admission(aid)["version"], len(r.facts(aid, live_only=False))) == before
    api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"})
    assert r.store.get(m.QUESTIONS, q["id"])["status"] == "answered"


def test_hold_it_adds_nothing_and_throws_the_set_aside_values_away(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    out = api.post(f"/questions/{q['id']}/answer", json={"answer": "Hold it"}).json()
    assert out["added"] == 0 and not out["values_not_added"]
    assert [f["value"]["text"] for f in r.facts(aid)] == ["88/min"]
    assert r.store.get(m.PAGES, q["page_id"])["held"] is True
    waiting = r.store.get(m.HELD, m.stable_id("held", q["page_id"]))
    assert waiting["status"] == "kept_out" and waiting["candidates"] == []


def test_alerts_run_for_values_added_by_the_answer_and_not_for_hold_it(tmp_path, monkeypatch):
    api, r, aid = world(tmp_path)
    calls = []
    monkeypatch.setattr(api.app.state.alerts, "after_merge", lambda admission_id, before, **kw: calls.append(admission_id))
    q = held_question(r, aid)
    api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"})
    assert calls == [aid]
    api2, r2, aid2 = world(tmp_path / "second")
    calls2 = []
    monkeypatch.setattr(api2.app.state.alerts, "after_merge", lambda admission_id, before, **kw: calls2.append(1))
    api2.post(f"/questions/{held_question(r2, aid2)['id']}/answer", json={"answer": "Hold it"})
    assert calls2 == []


def test_a_question_held_before_this_fix_says_plainly_that_nothing_can_be_added(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    r.store.put(m.QUESTIONS, {k: v for k, v in q.items() if k != "values_kept"})
    import sqlite3
    with sqlite3.connect(str(r.store.path)) as conn:
        conn.execute("DELETE FROM docs WHERE coll = ?", (m.HELD,))
    row = api.get(f"/admissions/{aid}/today").json()["questions"][0]
    assert "cannot be added" in row["note"] and [o["value"] for o in row["options"]] == ["Hold it", "It is this patient"]
    out = api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"}).json()
    assert out["added"] == 0 and out["values_not_added"] is True
    assert [f["value"]["text"] for f in r.facts(aid)] == ["88/min"]
    assert r.store.get(m.QUESTIONS, q["id"])["status"] == "answered"


def test_new_held_question_row_has_no_note_so_the_video_sheet_is_unchanged():
    q = {"id": "q1", "kind": m.Q_WRONG_PATIENT, "text": "x (UHID differs)", "fact_ids": [], "page_id": None,
         "source_id": "src1", "values_kept": True}
    row = question_rows([q], {}, {}, {})[0]
    assert row["text"] == "This page shows a different UHID. Hold it?" and row["source_id"] == "src1"
    assert row["note"] == "" and [o["value"] for o in row["options"]] == ["Hold it", "It is this patient"]


def test_a_guardian_or_attendant_line_does_not_hold_the_patients_own_page(tmp_path):
    api, r, aid = world(tmp_path, extra_note="Name: Test Patient\nGuardian Name : Omkar\nAttendant Name : Omkar\n"
                                              "Date 21/10\nPR 99/min")
    assert r.questions(aid) == []
    assert sorted(f["value"]["text"] for f in r.facts(aid)) == ["88/min", "99/min"]
    api2, r2, aid2 = world(tmp_path / "other", extra_note="Name: Ramesh Chandra Joshi\nGuardian Name : Test Patient\n"
                                                         "Date 21/10\nPR 99/min")
    assert held_question(r2, aid2)["kind"] == m.Q_WRONG_PATIENT


def test_split_flag_takes_only_a_letter_beside_a_number():
    assert split_flag("H 426", 426) == ("426", "H")
    assert split_flag("H426", 426) == ("426", "H")
    assert split_flag("L 3.2 mmol/L", 3.2) == ("3.2 mmol/L", "L")
    assert split_flag("426 H", 426) == ("426", "H")
    assert split_flag("426 (H)", 426) == ("426", "H")
    assert split_flag("9.8 g/dl L") == ("9.8 g/dl", "L")
    assert split_flag("HH 12.5", 12.5) == ("12.5", "HH")
    for untouched in ("H pylori positive", "Positive", "L 4-5 disc", "426", "NIL", "H", "Hb 9", "Reactive", ""):
        assert split_flag(untouched) == (untouched, None)
    assert split_flag("H 426", 99) == ("H 426", None)


def test_a_new_urea_is_stored_without_the_flag_letter_and_the_number_is_unchanged():
    raw = {"facts": [{"p": 1, "t": "lab_result", "k": "urea", "v": "H 426", "n": 426, "u": "mg/dl", "q": "Urea H 426"},
                     {"p": 1, "t": "lab_result", "k": "h pylori", "v": "H pylori positive", "q": "H pylori positive"},
                     {"p": 1, "t": "observation", "k": "spine", "v": "L 4-5 tender", "q": "L 4-5 tender"}]}
    page = {"id": "pg", "source_id": "s", "text": "Urea H 426 mg/dl\nH pylori positive\nL 4-5 tender"}
    urea, pylori, spine = to_candidates(raw, [page], admission={"id": "a"})
    assert urea["value"]["text"] == "426" and urea["value"]["flag"] == "H" and urea["value"]["number"] == 426
    assert urea["quote"] == "Urea H 426" and urea["quote_found"]
    assert pylori["value"]["text"] == "H pylori positive" and "flag" not in pylori["value"]
    assert spine["value"]["text"] == "L 4-5 tender"


def test_an_old_record_with_h_426_is_shown_right_without_rewriting_it():
    old = {"id": "f1", "type": "lab_result", "key": "urea", "status": "active",
           "value": {"text": "H 426", "number": 426.0, "unit": None}}
    assert _show(old) == "426" and old["value"]["text"] == "H 426"
    assert outside(old)[0] == "high"
    text_result = {"id": "f2", "type": "lab_result", "key": "h pylori", "status": "active",
                   "value": {"text": "H pylori positive", "number": None}}
    assert _show(text_result) == "H pylori positive"


def test_the_screen_shows_426_for_both_old_and_new_urea(tmp_path):
    api, r, aid = world(tmp_path)
    q = held_question(r, aid)
    api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"})
    old = [f for f in r.facts(aid) if f["key"] == "urea"][0]
    r.store.put(m.FACTS, dict(old, value=dict(old["value"], text="H 426")))
    page = api.get(f"/admissions/{aid}/today").json()
    shown = [row["value"] for row in page["labs"] if row["key"] == "urea"]
    assert shown == ["426"]
