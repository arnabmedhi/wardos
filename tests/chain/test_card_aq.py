"""The server side of the patient screen: news line, what changed, past values, record in summary order."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from wardos.chain.record_view import record_sections
from wardos.chain.today import build_today

from .test_api import _client
from .test_beds import stay
from .test_today import ADM, PATIENT, fact

DAY = date(2026, 10, 21)


def page(facts, questions=(), admission=ADM):
    return build_today(admission, PATIENT, list(facts), list(questions), today=DAY)


def lab(key, text, when, number=None, **kw):
    return fact("lab_result", key, text, when, number if number is not None else float(text), **kw)


def test_changes_are_ranked_danger_new_worse_vital_better():
    t = page([lab("potassium", "4.2", "2026-10-20"), lab("potassium", "7.5", "2026-10-21"),
              lab("sodium", "138", "2026-10-20"), lab("sodium", "130", "2026-10-21"),
              lab("urea", "47", "2026-10-20"), lab("urea", "52", "2026-10-21"),
              fact("vital", "temperature", "98.6", "2026-10-20", 98.6), fact("vital", "temperature", "99.8", "2026-10-21", 99.8),
              lab("creatinine", "2.0", "2026-10-20"), lab("creatinine", "1.5", "2026-10-21"),
              lab("chloride", "100", "2026-10-20"), lab("chloride", "101", "2026-10-21")])
    assert [c["key"] for c in t["changes"]] == ["potassium", "sodium", "urea", "temperature", "creatinine"]
    assert [c["rank"] for c in t["changes"]] == [9, 5, 4, 3, 1]
    assert t["changes"][4]["good"] and not t["changes"][4]["bad"]
    assert t["news"] == "1 alert, 4 changes"


def test_low_to_high_is_never_better():
    t = page([lab("sodium", "120", "2026-10-20"), lab("sodium", "147", "2026-10-21")])
    assert t["changes"][0]["bad"] and not t["changes"][0]["good"]


def test_every_alert_is_on_the_first_page_so_row_and_page_agree():
    first = page([lab("potassium", "7.5", "2026-10-21")])
    assert [c["key"] for c in first["changes"]] == ["potassium"] and first["changes"][0]["was"] is None
    same = page([lab("potassium", "7.5", "2026-10-20"), lab("potassium", "7.5", "2026-10-21")])
    assert len(same["alerts"]) == 1 and [c["key"] for c in same["changes"]] == ["potassium"] and same["news"] == "1 alert"


def test_news_says_the_quiet_cases_plainly():
    assert page([])["news"] == "Nothing read yet"
    assert page([lab("chloride", "100", "2026-10-21")])["news"] == "No recent changes"
    assert page([lab("chloride", "100", "2026-10-21")], admission={**ADM, "severity": "unstable"})["news"] == "Marked unstable"
    old = page([lab("sodium", "120", "2026-10-01"), lab("sodium", "118", "2026-10-02"), lab("chloride", "100", "2026-10-21")])
    assert old["changes"] == [] and old["news"] == "No recent changes"


def test_past_values_keep_the_short_form_the_written_words_and_the_photo():
    t = page([lab("tlc", "13400", "2026-10-19"), lab("tlc", "12.1", "2026-10-20"), lab("tlc", "16.32", "2026-10-21")])
    row = next(x for x in t["labs"] if x["key"] == "tlc")
    assert [s["v"] for s in row["series"]] == [13.4, 12.1, 16.32] and row["range"] == [4, 11]
    assert [s["text"] for s in row["series"]] == ["13400", "12.1", "16.32"] and row["series"][0]["day"] == "19 Oct"
    assert all(s["fact_id"] and s["source_id"] for s in row["series"])


def test_a_copied_value_is_one_reading_and_a_written_result_has_no_graph():
    t = page([lab("urea", "40", "2026-10-20"), lab("urea", "40", "2026-10-20"),
              fact("lab_result", "hiv", "NON REACTIVE", "2026-10-21")])
    rows = {r["key"]: r for r in t["labs"]}
    assert len(rows["urea"]["series"]) == 1 and rows["hiv"]["series"] == []


def test_record_is_in_summary_order_and_leaves_out_a_course_it_does_not_have():
    facts = [fact("diagnosis", "aki", "Acute kidney injury", "2026-10-21"), fact("complaint", "vomiting", "Vomiting 2 days", "2026-10-20"),
             fact("history", "dm", "No DM", "2026-10-20"), fact("observation", "air entry", "equal", "2026-10-21"),
             fact("vital", "pulse", "88", "2026-10-21", 88), lab("sodium", "130", "2026-10-21"),
             fact("med_event", "ceftriaxone", "Inj Ceftriaxone 1 g", "2026-10-21"), fact("plan", "repeat", "Repeat KFT", "2026-10-21")]
    sections = record_sections(facts, page(facts))
    assert [s["heading"] for s in sections] == ["Diagnosis", "Complaints", "History", "Examination and vitals", "Investigations", "Treatment", "Plan"]
    words = {s["heading"]: [x["text"] for x in s["lines"]] for s in sections}
    assert words["Examination and vitals"] == ["Pulse 88 /min", "Air entry: equal"]
    assert words["Plan"] == ["Repeat KFT"] and "Sodium 130 mmol/L" in words["Investigations"]
    assert all(x["fact_id"] for s in sections for x in s["lines"])


def test_course_comes_only_from_real_dated_events():
    facts = [fact("procedure", "ercp", "ERCP done", "2026-10-20"), fact("observation", "hospital course", "Improved on antibiotics", "2026-10-21"),
             fact("decision", "referral", "Referred to surgery", "2026-10-21")]
    course = next(s for s in record_sections(facts, page(facts)) if s["heading"] == "Course")
    assert [x["text"].split(":")[0] for x in course["lines"]] == ["20 Oct", "21 Oct", "21 Oct"]
    exam = next(s for s in record_sections(facts, page(facts)) if s["heading"] == "Examination and vitals")
    assert exam["lines"] == []


def test_ward_row_and_patient_page_say_the_same_thing_over_the_routes(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Rina", "5")["id"]
    api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 88/min"})
    api.post(f"/admissions/{a}/notes", json={"text": "Date 21/10\nPR 130/min"})
    row = api.get("/admissions").json()[0]["row"]
    today = api.get(f"/admissions/{a}/today").json()
    assert row["news"] == today["news"] == "1 alert" and today["changes"][0]["key"] == "pulse"
    assert row["needs"] == 0 and row["job"] is None and row["watching"] is None
    assert [s["heading"] for s in api.get(f"/admissions/{a}/record").json()["sections"]][:2] == ["Diagnosis", "Complaints"]
    assert api.get("/admissions/nope/record").status_code == 404


def test_robot_status_reaches_the_row_and_the_page(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Rina", "5")["id"]
    desk = api.app.state.agent
    task = desk.create("get_reports", admission_id=a, bed="5", doctor="d", first_line="Waiting for the laptop worker")
    desk.create_watch(admission_id=a, bed="5", test="potassium", until=datetime.now(timezone.utc) + timedelta(hours=8), every_min=15, doctor="d")
    row = api.get("/admissions").json()[0]["row"]
    today = api.get(f"/admissions/{a}/today").json()
    assert row["job"] == today["job"] == "Fetching reports" and row["job_id"] == task["id"]       # the job, never the robot's step
    assert row["watching"] == today["watching"] == "potassium" and today["watch_id"]


def test_mission_page_and_its_feed_are_gone(tmp_path):
    api = _client(tmp_path)
    assert api.get("/mission").status_code == 404 and api.get("/events/recent").status_code == 404
