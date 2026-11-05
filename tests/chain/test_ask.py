"""The ask bar."""
from __future__ import annotations

import pytest

from wardos.chain import ask as asking
from wardos.chain import model as m

from .test_api import _client

ARRIVED = "2026-10-24T05:00:00+00:00"


def _lab(rec, adm, src, key, text, number, day, unit="", index=0, label=None, flag=None):
    return m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=index, type="lab_result", key=key,
                      label=label or key, value={"text": text, "number": number, "unit": unit, "flag": flag},
                      clinical_time=day, time_precision="day", assertion="present", quote=f"{key} {text}",
                      quote_found=True)


def _stay(api, name, bed, labs=(), pulse=None, meds=(), plan=()):
    rec = api.app.state.record
    patient = api.post("/patients", json={"name": name}).json()
    adm = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Medicine Unit 1", "admitted_at": "2026-10-20",
                                        "bed": bed}).json()
    src, _ = rec.add_source(adm["id"], "text", text=f"note {name}", received_at=ARRIVED)
    rec.save_pages(src["id"], [{"number": 1, "text": "x"}])
    facts, i = [], 0
    for key, text, number, day, *rest in labs:
        facts.append(_lab(rec, adm, src, key, text, number, day, rest[0] if rest else "", i)); i += 1
    if pulse:
        facts.append(m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=i, type="vital",
                                key="pulse", label="Pulse", value={"text": str(pulse), "number": float(pulse)},
                                clinical_time="2026-10-23", time_precision="day", assertion="present",
                                quote=f"PR {pulse}", quote_found=True)); i += 1
    for label, text in meds:
        facts.append(m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=i, type="med_event",
                                key=label.lower(), label=label, value={"text": text, "event": "started"},
                                clinical_time="2026-10-22", time_precision="day", assertion="present", quote=text,
                                quote_found=True)); i += 1
    for text in plan:
        facts.append(m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=i, type="plan",
                                key=text.lower(), label=text, value={"text": text}, clinical_time="2026-10-23",
                                time_precision="day", assertion="present", quote=text, quote_found=True)); i += 1
    for f in facts:
        rec.store.put(m.FACTS, f)
    return adm["id"]


@pytest.fixture
def ward(tmp_path):
    api = _client(tmp_path)
    ids = {
        "five": _stay(api, "Ramesh Kumar Verma", "5",
                      labs=[("tlc", "5.9", 5.9, "2026-10-22"), ("tlc", "16.3", 16.3, "2026-10-23"),
                            ("sodium", "139", 139, "2026-10-23", "mmol/L"), ("sgpt", "95", 95, "2026-10-23"),
                            ("haemoglobin", "13.5", 13.5, "2026-10-23"), ("creatinine", "0.9", 0.9, "2026-10-23")],
                      pulse=96, meds=[("Inj. Pantop", "Inj Pantop 40 mg IV OD"), ("Tab. Metformin", "Tab Metformin 500 mg BD")],
                      plan=["CT abdomen tomorrow"]),
        "seven": _stay(api, "Sumedha Ranjika", "7", labs=[("potassium", "6.4", 6.4, "2026-10-23")]),
        "twelve": _stay(api, "Aashvi Devi", "12"),
        "twin": _stay(api, "Aashvi Singh", "14"),
    }
    return api, ids


def _ask(api, text, current=None):
    body = {"text": text, "direct": True, **({"admission_id": current} if current else {})}   # the record answer itself, not the command reader
    res = api.post("/ask", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_the_ten_asks_from_the_card_pick_the_right_action_and_patient(ward):
    api, ids = ward
    table = [
        ("bed 5 reports", "reports", "five"),
        ("show Verma's liver tests", "tests", "five"),
        ("vitals", "vitals", "five"),
        ("what medicines is bed 5 on", "medicines", "five"),
        ("plan for Sumedha", "plan", "seven"),
        ("photo of the TLC", "photo", "five"),
        ("make discharge summary for bed 5", "summary", "five"),
        ("qwerty zzz", "anything", "five"),
        ("lowest tlc for bed 5 and when", "anything", "five"),
        ("which potassium supplements is Sumedha on", "anything", "seven"),
    ]
    for text, action, who in table:
        res = _ask(api, text, ids["five"])
        assert res["intent"]["action"] == action, text
        if who:
            assert res["intent"]["patient_id"] == ids[who], text


def test_hindi_english_variants_and_misspellings(ward):
    api, ids = ward
    for text, action in [("bed 5 dawai", "medicines"), ("bed 5 davai kya hai", "medicines"), ("bed 5 vitel", "vitals"),
                         ("bed 5 BP", "vitals"), ("bed 5 janch", "tests"), ("bed 5 riport", "reports"),
                         ("bed 5 summery", "summary"), ("bed 5 TLC", "tests"), ("bed 5 kaisa hai", "brief"),
                         ("bed number 5 pending", "plan"), ("5 number bed ki report", "reports"),
                         ("बेड 5 दवा", "medicines")]:
        res = _ask(api, text)
        assert res["intent"]["action"] == action and res["intent"]["patient_id"] == ids["five"], text


def test_reports_come_in_the_order_of_the_paper_file(ward):
    api, ids = ward
    card = _ask(api, "bed 5 reports")["card"]
    assert card["kind"] == "reports" and card["day"] == "2026-10-23"
    assert [g["name"] for g in card["groups"]] == ["CBC", "Kidney tests and electrolytes", "Liver tests"]
    assert [r["label"] for r in card["groups"][0]["rows"]] == ["Hb", "TLC"]
    liver = _ask(api, "bed 5 liver reports")["card"]
    assert [g["name"] for g in liver["groups"]] == ["Liver tests"]


def test_reports_for_a_named_day(ward):
    api, ids = ward
    for text in ("bed 5 reports 22 oct", "bed 5 reports 22/10", "bed 5 pichla report", "reports oct 22 bed 5"):
        card = _ask(api, text)["card"]
        assert card["day"] == "2026-10-22", text
        assert [r["label"] for r in card["groups"][0]["rows"]] == ["TLC"]
        assert card["groups"][0]["rows"][0]["range"] == [4, 11]
    missing = _ask(api, "bed 5 reports 3 oct")["card"]
    assert missing["missing_day"] == "2026-10-03" and missing["day"] == "2026-10-23"


def test_tests_show_abnormal_first_with_the_table_behind(ward):
    api, ids = ward
    card = _ask(api, "bed 5 tests")["card"]
    assert card["kind"] == "tests" and not card["filtered"]
    assert [r["label"] for r in card["abnormal"]] == ["TLC", "SGPT"]
    assert {r["label"] for r in card["in_range"]} == {"Hb", "Sodium", "Creatinine"}
    table = card["table"]
    assert table["dates"] == ["2026-10-23", "2026-10-22"]
    tlc = next(r for r in table["rows"] if r["key"] == "tlc")
    assert [c["value"] if c else None for c in tlc["cells"]] == ["16.3", "5.9"]
    assert tlc["cells"][0]["dir"] == "high" and tlc["cells"][1]["dir"] is None
    assert all(c is None or c["source_id"] for r in table["rows"] for c in r["cells"])
    assert [r["group"] for r in table["rows"]] == sorted(r["group"] for r in table["rows"])
    liver = _ask(api, "show Verma's liver tests")["card"]
    assert liver["filtered"] and liver["title"] == "Liver tests" and [r["label"] for r in liver["abnormal"]] == ["SGPT"]
    one = _ask(api, "bed 5 TLC")["card"]
    assert one["filtered"] and one["title"] == "TLC" and [r["key"] for r in one["table"]["rows"]] == ["tlc"]


def test_vitals_medicines_plan_and_brief_cards(ward):
    api, ids = ward
    vitals = _ask(api, "bed 5 vitals")["card"]
    assert vitals["kind"] == "vitals" and vitals["rows"][0]["label"] == "Pulse" and vitals["rows"][0]["value"] == "96"
    meds = _ask(api, "what medicines is bed 5 on")["card"]
    assert [g["name"] for g in meds["groups"]] == ["Injections", "Tablets and syrups"]
    assert meds["groups"][0]["rows"][0]["label"] == "Inj. Pantoprazole" and meds["stopped"] == []
    plan = _ask(api, "bed 5 plan")["card"]
    assert plan["kind"] == "plan" and plan["rows"][0]["text"] == "CT abdomen tomorrow"
    brief = _ask(api, "bed 5")["card"]
    assert brief["kind"] == "brief" and len(brief["lines"]) == 4
    assert "Bed 5" in brief["lines"][0]
    empty = _ask(api, "bed 12 vitals")["card"]
    assert empty["kind"] == "message" and "No vitals" in empty["text"]


def test_photo_card_points_at_the_page_behind_the_value(ward):
    api, ids = ward
    card = _ask(api, "photo of the TLC", ids["five"])["card"]
    assert card["kind"] == "photo" and card["row"]["label"] == "TLC" and card["row"]["value"] == "16.3"
    assert card["row"]["source_id"] and card["row"]["quote"] == "tlc 16.3"
    nothing = _ask(api, "photo of the ferritin", ids["five"])["card"]
    assert nothing["kind"] == "answer"
    assert _ask(api, "photo", ids["five"])["card"]["text"].startswith("Which value?")


def test_summary_stops_at_the_confirm(ward):
    api, ids = ward
    card = _ask(api, "make discharge summary for bed 7")["card"]
    assert card["kind"] == "confirm" and card["text"] == "Make the discharge summary for Bed 7? It takes about 4-6 minutes."
    assert card["confirm"] == "summary" and card["patient"]["id"] == ids["seven"]
    assert not api.get(f"/admissions/{ids['seven']}/final").json()


def test_patient_matching_bed_name_current_and_misses(ward):
    api, ids = ward
    assert _ask(api, "bed 99 vitals")["card"]["text"] == "I can't find Bed 99 on your list."
    assert _ask(api, "vitals")["card"]["text"].startswith("Which patient?")
    twin = _ask(api, "reports for Aashvi")["card"]
    assert twin["kind"] == "choose" and {c["id"] for c in twin["choices"]} == {ids["twelve"], ids["twin"]}
    assert twin["retry_text"] == "reports"
    assert _ask(api, "reports for Aashvi Singh")["intent"]["patient_id"] == ids["twin"]
    assert _ask(api, "reports for Singh")["intent"]["patient_id"] == ids["twin"]
    assert _ask(api, "bed 7 tests", ids["five"])["intent"]["patient_id"] == ids["seven"]
    assert _ask(api, "sumedha's potassium")["card"]["abnormal"][0]["label"] == "Potassium"
    assert api.post("/ask", json={"text": "  "}).status_code == 400


def test_unknown_lists_what_can_be_shown(ward):
    api, ids = ward
    card = _ask(api, "play some music")["card"]
    assert card["kind"] == "message" and card["text"].endswith("I can show: reports, tests, vitals, medicines, plan, summary")


def test_a_note_is_stored_for_the_named_patient(ward):
    api, ids = ward
    before = len(api.get(f"/admissions/{ids['seven']}/timeline").json())
    res = _ask(api, "note bed 7 Date 23/10 PR 101/min", ids["five"])
    assert res["intent"]["action"] == "note" and res["card"]["kind"] == "message"
    assert res["card"]["text"] == "Note saved for Bed 7." and res["card"]["source_id"]
    assert len(api.get(f"/admissions/{ids['seven']}/timeline").json()) == before + 1
    again = _ask(api, "note bed 7 Date 23/10 PR 101/min", ids["five"])
    assert again["card"]["text"] == "That note is already in the record."
    assert _ask(api, "note", ids["five"])["card"]["text"].startswith("Say the note")


def test_asks_never_call_the_model(tmp_path):
    from fastapi.testclient import TestClient
    from wardos.chain.api import create_app
    from wardos.chain.docstore import SQLiteDocStore
    from wardos.chain.files import LocalFileStore
    from wardos.chain.record import Record
    from .test_api import SETTINGS, InlineQueue

    class NoModel:
        async def generate(self, *a, **k):
            raise AssertionError("an ask must not call the model")
    queue = InlineQueue()
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")), model=NoModel(),
                     settings=SETTINGS, queue=queue)
    queue.app = app
    api = TestClient(app)
    _stay(api, "Ramesh Kumar Verma", "5", labs=[("tlc", "16.3", 16.3, "2026-10-23")])
    for text in ("bed 5 reports", "bed 5 tests", "bed 5 vitals", "bed 5 medicines", "bed 5 plan", "bed 5",
                 "photo of the TLC", "make discharge summary for bed 5", "get reports for bed 5", "nonsense"):
        assert api.post("/ask", json={"text": text, "direct": True}).status_code == 200


def test_new_patient_by_voice_reads_the_details_and_writes_nothing(ward):
    api, ids = ward
    before = len(api.get("/admissions").json())
    res = _ask(api, "new patient bed 12 Ramesh Kumar 45 years male UHID 2026 0999 052 medicine unit 1")
    card = res["card"]
    assert res["intent"]["action"] == "new_patient" and card["kind"] == "new_patient"
    f = card["fields"]
    assert (f["bed"], f["name"], f["age"], f["sex"], f["uhid"], f["unit"]) == \
        ("12", "Ramesh Kumar", "45", "M", "20260999052", "Medicine Unit 1")
    assert len(api.get("/admissions").json()) == before
    hindi = _ask(api, "naya patient bed 5 naam Mohd Salim 38 saal purush")["card"]["fields"]
    assert (hindi["name"], hindi["age"], hindi["sex"]) == ("Mohd Salim", "38", "M")
    assert hindi["bed_taken_by"]
    bare = _ask(api, "new patient")["card"]
    assert bare["kind"] == "message" and "bed" in bare["text"] and "fields" not in bare
    said = _ask(api, "new patient bed 16 Ramesh Kumar 45 male fever")["card"]["fields"]
    assert (said["name"], said["age"], said["sex"], said["bed"]) == ("Ramesh Kumar", "45", "M", "16")
    assert _ask(api, "naya patient bed 17 Aashvi Devi 30 saal mahila bukhar")["card"]["fields"]["name"] == "Aashvi Devi"
    after = _ask(api, "new patient 45 male Ramesh Kumar bed 12")["card"]["fields"]
    assert after["name"] == "Ramesh Kumar"
    assert _ask(api, "add photos for bed 5")["intent"]["action"] == "photo"
    assert _ask(api, "add note bed 5 plan CT")["intent"]["action"] == "note"


def test_a_name_nobody_has_says_not_found(ward):
    api, _ = ward
    card = _ask(api, "xyzabc")["card"]
    assert card["kind"] == "message" and card["title"] == "Not found" and "Nobody" in card["text"]


def test_photo_of_a_panel_name_shows_the_newest_page_of_that_panel():
    from wardos.chain.ask import _panel_row
    page = {"labs": [{"key": "haemoglobin", "time": "2026-10-11", "source_id": "s1"},
                     {"key": "platelets", "time": "2026-10-22T12:03", "source_id": "s2"},
                     {"key": "creatinine", "time": "2026-10-23", "source_id": "s3"},
                     {"key": "tlc", "time": "2026-10-23", "source_id": None}]}
    assert _panel_row(page, "show me the photo of the CBC for bed 5")["source_id"] == "s2"
    assert _panel_row(page, "photo of the kidney tests")["source_id"] == "s3"
    assert _panel_row(page, "photo of the TLC") is None and _panel_row(page, "photo of the LFT") is None


def test_a_patient_added_by_voice_can_get_photos_later(ward):
    api, _ = ward
    pt = api.post("/patients", json={"name": "Ramesh Kumar", "uhid": None, "sex": "M", "age": "45"}).json()
    adm = api.post("/admissions", json={"patient_id": pt["id"], "unit": "Medicine Unit 1", "bed": "12"}).json()
    row = next(x for x in api.get("/admissions").json() if x["admission"]["id"] == adm["id"])["row"]
    assert (row["bed"], row["age"], row["sex"]) == ("12", 45, "male")


async def test_ask_anything_answers_from_the_record_with_its_sources(ward):
    from wardos.chain.askai import answer_anything, pick_entries
    from wardos.models import Usage
    api, ids = ward
    rec = api.app.state.record
    assert asking.ask(rec, "was consent taken for bed 5", ids["five"])["card"]["kind"] == "ai"
    seen = {}

    class Answers:
        async def generate(self, prompt, **kwargs):
            seen["prompt"] = prompt
            line = next(ln for ln in prompt.splitlines() if "| tlc |" in ln and "16.3" in ln)
            return {"answer": "TLC was 16.3 on 23 Oct.", "used": [line.split(" | ")[0], "E999"]}, Usage("fake", cost_inr=0.4)
    block = {"id": ids["five"], "label": "Bed 5, Ramesh", "bed_label": "Bed 5"}
    card = await answer_anything(rec, Answers(), "fake", block, "what was the TLC")
    assert card["kind"] == "answer" and card["text"] == "TLC was 16.3 on 23 Oct."
    assert [s["value"] for s in card["sources"]] == ["16.3"] and card["sources"][0]["fact_id"]
    assert card["cost_inr"] == 0.4 and "Question: what was the TLC" in seen["prompt"]
    many = [{"id": f"f{i}", "type": "lab_result", "key": "sodium", "label": "Sodium", "clinical_time": f"2026-10-{i:02d}"}
            for i in range(1, 21)]
    assert len(pick_entries(many, "any fever")) == 4
    assert len(pick_entries(many, "sodium trend")) == 20
