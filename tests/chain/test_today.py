"""The patient page worked out in code: normal ranges, the top card rule and the ward list."""
from __future__ import annotations

from datetime import date

from wardos.chain import model as m
from wardos.chain.today import (build_today, danger_limits, fold_lines, latest_and_previous, normal_range, outside,
                                shown_range, ward_flag)

from .test_api import _client

ADM = {"id": "adm_x", "unit": "Medicine Unit 1", "admitted_at": "2026-10-18", "version": 3}
PATIENT = {"id": "pat_x", "name": "AASHVI DEVI", "sex": "F", "age": "58"}
_n = [0]


def fact(type_, key, text, when, number=None, unit=None, flag=None, reference=None, status=m.ACTIVE, label=None):
    _n[0] += 1
    return {"id": f"fact_{_n[0]}", "admission_id": "adm_x", "type": type_, "key": key, "label": label or key,
            "value": {"text": text, "number": number, "unit": unit, "flag": flag, "reference": reference},
            "clinical_time": when, "time_precision": "day" if when and len(when) == 10 else "exact",
            "assertion": "present", "status": status, "source_id": "src_1", "page_id": None, "quote": text,
            "quote_found": True, "created_at": f"2026-10-20T00:00:{_n[0]:02d}"}


def test_normal_range_prefers_the_labs_own_reference_then_the_table_by_sex():
    assert normal_range(fact("lab_result", "direct bilirubin", "0.22 H", "2026-10-20", 0.22,
                             reference="0.1 - 0.2")) == (0.1, 0.2)
    assert normal_range(fact("lab_result", "haemoglobin", "11.2", "2026-10-20", 11.2), "f") == (12.0, 15.0)
    assert normal_range(fact("lab_result", "haemoglobin", "11.2", "2026-10-20", 11.2), "m") == (13.0, 17.0)
    assert normal_range(fact("lab_result", "ferritin", "300", "2026-10-20", 300)) is None


def test_outside_reads_ward_short_forms():
    assert outside(fact("lab_result", "tlc", "13.4", "2026-10-20", 13.4))[0] == "high"
    assert outside(fact("lab_result", "tlc", "8,200", "2026-10-20"))[0] is None
    assert outside(fact("lab_result", "platelets", "1.2 lakh", "2026-10-20", 1.2))[0] == "low"
    assert outside(fact("lab_result", "platelets", "137 K", "2026-10-20"))[0] == "low"
    assert outside(fact("vital", "temperature", "38.5 C", "2026-10-20", 38.5))[0] == "high"
    assert outside(fact("vital", "blood pressure", "86/50", "2026-10-20"))[0] == "low"
    assert outside(fact("lab_result", "hiv", "NON REACTIVE", "2026-10-20"))[0] is None
    assert outside(fact("lab_result", "hepatitis c", "Positive", "2026-10-20"))[0] == "abnormal"
    assert outside(fact("lab_result", "ggt", "80 H", "2026-10-20", 80, flag="H"))[0] == "high"


def test_the_range_on_screen_is_in_the_same_short_form_as_the_value():
    assert shown_range(fact("lab_result", "tlc", "16.32", "2026-10-20", 16.32)) == [4, 11]
    assert shown_range(fact("lab_result", "tlc", "8,200", "2026-10-20")) == [4000, 11000]
    assert shown_range(fact("lab_result", "platelets", "108", "2026-10-20", 108)) == [150, 450]
    assert shown_range(fact("lab_result", "platelets", "1.2 lakh", "2026-10-20", 1.2)) == [1.5, 4.5]
    assert shown_range(fact("lab_result", "sgpt", "48", "2026-10-20", 48)) == [None, 40]


def test_latest_and_previous_take_the_newest_from_an_earlier_day():
    facts = [fact("lab_result", "sodium", "138", "2026-10-19", 138), fact("lab_result", "sodium", "133", "2026-10-20", 133),
             fact("lab_result", "sodium", "135", "2026-10-20T06:00", 135),
             fact("lab_result", "sodium", "120", "2026-10-18", 120, status=m.SUPERSEDED)]
    latest, previous = latest_and_previous(facts, "sodium")
    assert latest["value"]["text"] == "135" and previous["value"]["text"] == "138"


def _page(facts, questions=()):
    return build_today(ADM, PATIENT, facts, list(questions), today=date(2026, 10, 21))


def test_danger_lines_are_two_sd_beyond_the_normal_limit():
    assert danger_limits(4000, 11000) == (500, 14500)
    assert danger_limits(135, 145) == (130, 150)
    assert danger_limits(3.5, 5.0) == (2.75, 5.75)
    assert danger_limits(150000, 450000) == (75000, 600000)
    assert danger_limits(None, 40) == (None, 60)
    low, high = danger_limits(12.0, 15.0)
    assert (low, high) == (10.5, 16.5)


def test_only_dangerous_values_are_alerts_and_mild_ones_are_red_text_only(monkeypatch):
    monkeypatch.setattr("wardos.chain.today.ALERT_BY_RANGE", False)      # these four check the old "two steps beyond the limit" rule
    facts = [
        fact("lab_result", "tlc", "9,100", "2026-10-19", 9100), fact("lab_result", "tlc", "13,400", "2026-10-20", 13400),
        fact("lab_result", "sodium", "131", "2026-10-19", 131), fact("lab_result", "sodium", "128", "2026-10-20", 128),
        fact("vital", "temperature", "101.2", "2026-10-19", 101.2), fact("vital", "temperature", "98.6", "2026-10-20", 98.6),
        fact("vital", "pulse", "92", "2026-10-19", 92), fact("vital", "pulse", "118", "2026-10-20", 118),
        fact("lab_result", "haemoglobin", "10.1", "2026-10-19", 10.1), fact("lab_result", "haemoglobin", "11.0",
                                                                               "2026-10-20", 11.0),
    ]
    page = _page(facts)
    assert [a["title"] for a in page["alerts"]] == ["Pulse past danger line", "Sodium low"]
    assert page["alerts"][1]["was"] == "131" and page["alerts"][1]["sub"] == "was 131"
    tlc = next(x for x in page["labs"] if x["key"] == "tlc")
    assert tlc["dir"] == "high" and tlc["danger"] is False
    assert page["flag"] == {"flag": "red", "reason": "Pulse past danger line 118 /min"}
    assert "top_cards" not in page

    q = [{"id": "q_1", "kind": "conflict", "fact_ids": [facts[0]["id"]], "text": "Different values", "created_at": "1"}]
    page = _page(facts, q)
    assert page["notices"][0]["kind"] == "need" and page["notices"][0]["value"] == "1"


def test_platelets_use_the_half_limit_floor(monkeypatch):
    monkeypatch.setattr("wardos.chain.today.ALERT_BY_RANGE", False)      # these four check the old "two steps beyond the limit" rule
    low = _page([fact("lab_result", "platelets", "0.6 lakh", "2026-10-20", 0.6)])
    mild = _page([fact("lab_result", "platelets", "1.2 lakh", "2026-10-20", 1.2)])
    assert [a["title"] for a in low["alerts"]] == ["Platelets low"]
    assert mild["alerts"] == [] and mild["labs"][0]["dir"] == "low" and mild["flag"]["flag"] is None


def test_mild_values_alone_give_no_alert_no_dot_and_a_quiet_card(monkeypatch):
    monkeypatch.setattr("wardos.chain.today.ALERT_BY_RANGE", False)      # these four check the old "two steps beyond the limit" rule
    facts = [fact("lab_result", "sodium", "133", "2026-10-20", 133), fact("vital", "pulse", "80", "2026-10-20", 80)]
    page = _page(facts)
    assert page["alerts"] == [] and page["notices"] == []
    assert page["flag"]["flag"] is None
    assert page["brief"]["lines"][2] == "Nothing dangerous"


def test_a_new_report_is_a_quiet_notice_unless_it_carries_a_danger():
    scan = fact("investigation_result", "usg abdomen", "Gallstones, no dilatation", "2026-10-20")
    page = _page([scan, fact("lab_result", "sodium", "139", "2026-10-20", 139)])
    assert [(n["kind"], n["title"]) for n in page["notices"]] == [("info", "New report: USG abdomen")]
    assert page["alerts"] == [] and page["flag"]["flag"] is None
    sources = [{"id": "src_pdf", "kind": "portal_report", "received_at": "2026-10-20T10:00:00+00:00"}]
    calm = dict(fact("lab_result", "sodium", "139", "2026-10-20", 139), source_id="src_pdf")
    page = build_today(ADM, PATIENT, [calm], [], sources=sources, today=date(2026, 10, 21))
    assert [n["title"] for n in page["notices"]] == ["New report: lab tests"] and page["alerts"] == []
    bad = dict(fact("lab_result", "sodium", "126", "2026-10-20", 126), source_id="src_pdf")
    page = build_today(ADM, PATIENT, [bad], [], sources=sources, today=date(2026, 10, 21))
    assert [a["title"] for a in page["alerts"]] == ["Sodium low"] and page["notices"] == []


def test_a_clinical_date_after_the_day_the_page_arrived_is_doubtful_and_never_latest():
    sources = [{"id": "src_1", "kind": "photo", "received_at": "2026-10-24T05:00:00+00:00"}]
    facts = [fact("vital", "pulse", "88", "2026-10-23", 88), fact("vital", "pulse", "140", "2026-10-26", 140),
             fact("lab_result", "sodium", "139", "2026-10-26", 139)]
    page = build_today(ADM, PATIENT, facts, [], sources=sources, today=date(2026, 10, 24))
    pulse = next(v for v in page["vitals"] if v["key"] == "pulse")
    assert pulse["value"] == "88" and pulse["danger"] is False
    assert page["as_of"] == "2026-10-23" and page["alerts"] == []
    sodium = page["labs"][0]
    assert sodium["value"] == "139" and sodium["when"] == "date unclear" and sodium["date_doubtful"] is True
    assert page["counts"]["doubtful_dates"] == 2
    ok = [dict(f, clinical_time="2026-10-25") for f in facts[:1]]
    assert build_today(ADM, PATIENT, ok, [], sources=sources, today=date(2026, 10, 24))["counts"]["doubtful_dates"] == 0


def test_day_counts_from_the_admission_date_to_the_real_date():
    page = build_today({"id": "adm_x", "admitted_at": "2026-10-21"}, PATIENT, [], [], today=date(2026, 10, 24))
    assert page["admission"]["day"] == 4 and page["admission"]["admitted_text"] == "Admitted 21 Oct (day 4)"


def test_medicine_names_groups_and_food():
    meds = [
        fact("med_event", "meropenem", "inj merol", "2026-10-20", label="inj merol"),
        fact("med_event", "pantoprazole", "Tab Pan 40", "2026-10-20", label="T. Pantop"),
        fact("med_event", "potassium chloride", "Inj KCl 10 meq/hr over 6 hrs in 500 ml NS", "2026-10-20",
             label="Inj KCl"),
        fact("med_event", "teicoplanin", "teicol inj", "2026-10-20", label="teicol inj"),
        fact("med_event", "iron supplement", "iron supplmt also", "2026-10-20", label="iron supplement"),
        fact("med_event", "protein supplement", "EGGS 2 PO TDS", "2026-10-20", label="Eggs"),
        fact("med_event", "xylitol", "XYLITOL GUM PO chewing gum TDS", "2026-10-20", label="Xylitol Gum"),
        fact("med_event", "packed red blood cells", "III PRBC Transfused", "2026-10-20", label="PRBC Transfusion"),
    ]
    for f in meds[:-2]:
        f["value"]["event"] = "given"
    meds[-1]["value"]["event"] = "given"
    meds[5]["value"]["event"] = meds[6]["value"]["event"] = "started"
    rows = {r["key"]: r for r in _page(meds)["medicines"]}
    assert "protein supplement" not in rows and "xylitol" not in rows
    assert rows["meropenem"]["label"] == "Inj. Meropenem" and rows["meropenem"]["group"] == "Injections"
    assert rows["pantoprazole"]["label"] == "Tab. Pantoprazole" and rows["pantoprazole"]["group"] == "Tablets and syrups"
    assert rows["teicoplanin"]["label"] == "Inj. Teicoplanin" and rows["teicoplanin"]["dose"] == ""
    assert rows["iron supplement"]["label"] == "Iron supplement" and rows["iron supplement"]["dose"] == ""
    assert rows["potassium chloride"]["group"] == "Drips, fluids and blood"
    assert rows["packed red blood cells"]["group"] == "Drips, fluids and blood"
    assert rows["packed red blood cells"]["label"] == "Packed red blood cells"


def test_diagnosis_noise_becomes_rule_out():
    from wardos.chain.normalize import clean_diagnosis
    assert clean_diagnosis("to rout MDS") == "Rule out MDS"
    assert clean_diagnosis("R/O TB") == "Rule out TB"
    assert clean_diagnosis("? Aplastic Anemia") == "Rule out Aplastic Anemia"
    assert clean_diagnosis("acute pancreatitis") == "Acute pancreatitis"
    facts = [fact("diagnosis", "mds", "to rout MDS", "2026-10-20"),
             fact("diagnosis", "anaemia", "Anemia with TCP", "2026-10-20"),
             fact("diagnosis", "lrti", "LRTI (resolved)", "2026-10-20")]
    facts[0]["assertion"] = "uncertain"
    assert _page(facts)["diagnosis"] == ["Anemia with TCP", "Rule out MDS"]


def test_brief_has_four_plain_lines(monkeypatch):
    monkeypatch.setattr("wardos.chain.today.ALERT_BY_RANGE", False)      # these four check the old "two steps beyond the limit" rule
    facts = [fact("admin", "bed", "5", "2026-10-21"), fact("lab_result", "tlc", "5.9", "2026-10-20", 5.9),
             fact("lab_result", "tlc", "12.1", "2026-10-21", 12.1),
             fact("diagnosis", "cholecystitis", "Acute cholecystitis", "2026-10-21"),
             fact("investigation_request", "usg", "USG abdomen", "2026-10-21", label="usg")]
    facts[-1]["assertion"] = "planned"
    page = build_today({"id": "adm_x", "admitted_at": "2026-10-21"}, {"name": "RAM LAL", "sex": "M", "age": "42"}, facts,
                       [], today=date(2026, 10, 24))
    assert page["brief"]["lines"] == ["Bed 5, RAM LAL, 42, male. Admitted 21 Oct (day 4)", "Acute cholecystitis",
                                      "Nothing dangerous", "USG pending since 21 Oct"]
    assert page["brief"]["text"].count("\n") == 3
    bare = build_today({"id": "adm_x"}, {"name": "AASHVI DEVI"}, [], [], today=date(2026, 10, 24))
    assert bare["brief"]["lines"] == ["AASHVI DEVI", "Diagnosis not written yet", "Nothing dangerous", "Nothing pending"]
    sick = [fact("lab_result", "sodium", "138", "2026-10-20", 138), fact("lab_result", "sodium", "126", "2026-10-21", 126)]
    page = build_today({"id": "adm_x"}, {"name": "AASHVI DEVI"}, sick, [], today=date(2026, 10, 24))
    assert page["brief"]["lines"][2] == "Dangerous: Sodium 138 → 126"


def test_two_values_on_one_page_make_a_full_sentence_question():
    a = fact("lab_result", "tlc", "7.8 K", "2026-10-21", 7.8, label="TLC")
    b = fact("lab_result", "tlc", "3.9 K", "2026-10-21", 3.9, label="TLC")
    for f in (a, b):
        f.update(page_id="page_1", status=m.DISPUTED)
    q = [{"id": "q_1", "kind": "conflict", "fact_ids": [a["id"], b["id"]], "text": "old", "created_at": "1",
          "page_id": "page_1", "source_id": "src_1"}]
    page = build_today(ADM, PATIENT, [a, b], q, pages_by_id={"page_1": {"id": "page_1", "page_type": "referral"}},
                       today=date(2026, 10, 21))
    row = page["questions"][0]
    assert row["text"] == ("The referral letter shows two TLC values, 7.8 K and 3.9 K, both read as 21 Oct. "
                           "Which one is the newest?")
    assert row["page_id"] == "page_1" and row["source_id"] == "src_1"
    assert all(o["page_id"] == "page_1" for o in row["options"])


def test_reports_come_back_in_the_order_of_the_paper_file():
    from wardos.chain.today import reports_in_order
    facts = [fact("lab_result", "bilirubin", "1.1", "2026-10-21", 1.1), fact("lab_result", "creatinine", "0.9", "2026-10-21", 0.9),
             fact("lab_result", "platelets", "2.1 lakh", "2026-10-21", 2.1), fact("lab_result", "haemoglobin", "11", "2026-10-21", 11),
             fact("lab_result", "inr", "1.1", "2026-10-21", 1.1), fact("lab_result", "ferritin", "300", "2026-10-21", 300),
             fact("lab_result", "sodium", "139", "2026-10-21", 139), fact("lab_result", "tlc", "8,000", "2026-10-21", 8000),
             fact("lab_result", "urine ph", "6", "2026-10-21", 6), fact("lab_result", "hba1c", "6.1", "2026-10-21", 6.1),
             fact("lab_result", "sodium", "135", "2026-10-18", 135)]
    out = reports_in_order(facts)
    assert out["day"] == "2026-10-21" and out["days"] == ["2026-10-21", "2026-10-18"]
    assert [(g["name"], [r["key"] for r in g["rows"]]) for g in out["groups"]] == [
        ("CBC", ["haemoglobin", "tlc", "platelets"]), ("Kidney tests and electrolytes", ["creatinine", "sodium"]),
        ("Liver tests", ["bilirubin"]), ("Clotting", ["inr"]), ("Sugar", ["hba1c"]), ("Urine", ["urine ph"]),
        ("Other tests", ["ferritin"])]
    older = reports_in_order(facts, "2026-10-18")
    assert older["day"] == "2026-10-18" and older["groups"][0]["rows"][0]["value"] == "135"


def test_ward_flag_and_fold_lines():
    assert ward_flag({"severity": "unstable", "severity_by": "dr_a"}, [], [])["flag"] == "red"
    q = [{"short": "BP"}, {"short": "Sodium"}]
    assert ward_flag({}, [], q) == {"flag": "amber", "reason": "2 answers needed: BP, Sodium"}
    assert ward_flag({}, [{"title": "TLC high", "value": "16.3", "unit": ""}], q) == \
        {"flag": "red", "reason": "TLC high 16.3"}
    facts = [fact("vital", "pulse", "88", "2026-10-20", 88), fact("vital", "blood pressure", "130/80", "2026-10-20"),
             fact("lab_result", "haemoglobin", "11.2", "2026-10-20", 11.2), fact("lab_result", "sodium", "140", "2026-10-20", 140)]
    page = _page(facts)
    lines = page["fold_lines"]
    assert [p["text"] for p in lines["vitals"]] == ["Pulse 88", "BP 130/80"]
    assert lines["tests"] == [{"text": "Hb 11.2", "word": "low", "bad": True}]
    assert lines["medicines"] == "No medicines yet"
    assert fold_lines([], [], [], [])["tests"] == [{"text": "No tests yet", "bad": False}]


def test_page_reads_bed_day_and_age_from_the_admission_page():
    facts = [fact("admin", "bed", "14", "2026-10-21"), fact("admin", "date of admission", "18/10/2026 11:00 AM",
                                                             "2026-10-18T11:00"),
             fact("identity", "age", "58Y 2M", None), fact("diagnosis", "acute pancreatitis", "Acute pancreatitis",
                                                           "2026-10-20")]
    page = build_today({"id": "adm_x", "reference_date": "2026-10-20"}, {"name": "AASHVI DEVI"}, facts, [],
                       today=date(2026, 10, 21))
    assert page["admission"]["bed"] == "14" and page["admission"]["admitted_on"] == "2026-10-18"
    assert page["admission"]["day"] == 4 and page["patient"]["age"] == 58
    assert page["diagnosis"] == ["Acute pancreatitis"]


def test_api_today_severity_and_answer_by_tapping_a_value(tmp_path):
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "RAM LAL"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2",
                                      "admitted_at": "2026-10-20"}).json()["id"]
    api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 88/min"})
    api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 120/min"})
    page = api.get(f"/admissions/{a}/today").json()
    assert page["notices"][0]["kind"] == "need" and len(page["questions"]) == 1
    assert page["questions"][0]["page_id"] and page["questions"][0]["source_id"]
    options = page["questions"][0]["options"]
    assert sorted(o["value"] for o in options) == ["120", "88"]
    ward = api.get("/admissions").json()[0]["row"]
    assert ward["flag"] == "amber" and ward["day"] >= 1 and ward["brief"]["lines"][0].startswith("RAM LAL")

    pick = next(o for o in options if o["value"] == "88")
    other = next(o for o in options if o["value"] == "120")
    assert api.post(f"/questions/{page['questions'][0]['id']}/answer",
                    json={"answer": "88/min", "fact_id": "fact_nope"}).status_code == 400
    done = api.post(f"/questions/{page['questions'][0]['id']}/answer", json={"answer": "88/min", "fact_id": pick["fact_id"]})
    assert done.status_code == 200
    live = api.get(f"/admissions/{a}/facts", params={"type": "vital"}).json()
    assert [(f["value"]["text"], f["status"]) for f in live] == [("88/min", "doctor_locked")]
    assert other["fact_id"] not in {f["id"] for f in live}
    assert api.get(f"/admissions/{a}/questions").json() == []
    assert api.get(f"/admissions/{a}/today").json()["flag"]["flag"] is None

    assert api.patch(f"/admissions/{a}/severity", json={"severity": "bad"}).status_code == 400
    set_ = api.patch(f"/admissions/{a}/severity", json={"severity": "unstable"})
    assert set_.json()["severity"] == "unstable" and set_.json()["severity_by"] == "dr_a"
    assert api.get("/admissions").json()[0]["row"]["flag"] == "red"


def test_a_bed_number_said_today_does_not_hide_yesterdays_danger():
    from wardos.chain.today import as_of_day
    facts = [fact("lab_result", "tlc", "16.3", "2026-10-22", 16.3),
             fact("admin", "bed", "5", "2026-10-25T00:57"), fact("identity", "patient name", "Aashvi Devi", "2026-10-25")]
    assert as_of_day(facts, date(2026, 10, 25)) == date(2026, 10, 22)


def test_a_wrong_patient_question_does_not_break_the_page():
    from wardos.chain.today import question_rows
    q = {"id": "q1", "kind": m.Q_WRONG_PATIENT, "text": "This page may be another patient's.", "fact_ids": [],
         "created_at": "2026-10-25T00:00:00"}
    row = question_rows([q], {}, {}, {})[0]
    assert row["source_id"] is None and [o["value"] for o in row["options"]] == ["Hold it", "It is this patient"]
    assert row["text"] == "This page shows a different name. Hold it?"
    q2 = dict(q, source_id="src1", text="Page 3 of this upload may belong to another patient (UHID differs). Keep it out of this record?")
    row2 = question_rows([q2], {}, {}, {})[0]
    assert row2["source_id"] == "src1" and row2["text"] == "This page shows a different UHID. Hold it?"
    old = dict(q, page_id="pg1")
    assert question_rows([old], {}, {"pg1": {"id": "pg1", "source_id": "src9"}}, {})[0]["source_id"] == "src9"


def test_standard_unit_in_grey_when_the_paper_wrote_none():
    from wardos.chain.today import standard_unit
    lab = lambda key, text, number: {"type": "lab_result", "key": key, "value": {"text": text, "number": number}}
    assert standard_unit(lab("tlc", "16.32", 16.32)) == "×10³/µL"
    assert standard_unit(lab("tlc", "16320", 16320)) == "/µL"
    assert standard_unit(lab("platelets", "1.5 lakh", 1.5)) == "lakh/µL"
    assert standard_unit(lab("sodium", "134", 134)) == "mmol/L"
    assert standard_unit(lab("ferritin", "40", 40)) == ""


def test_card_bg_clean_names_a_grade_is_not_the_diagnosis_and_blood_sugar_range():
    from wardos.chain import today as t
    assert t.clean_name("Hb aug - 8") == "Hb" and t.clean_name("CA19.9") == "CA19.9" and t.clean_name("Vit B12") == "Vit B12"
    assert t._unjunk("aug - 8") == "8" and t._unjunk("8.6") == "8.6" and t._unjunk("Negative") == "Negative"
    hb = {"type": "lab_result", "key": "haemoglobin", "label": "haemoglobin", "value": {"text": "aug - 8", "number": None, "unit": "g/dl"}}
    assert t._show(hb) == "8" and t.outside(hb)[0] == "low"                    # the number stays; only the date word goes
    assert all(t._GRADE_ONLY.match(x) for x in ("NYHA 4", "NYHA IV", "NYHA class 4"))
    assert not any(t._GRADE_ONLY.match(x) for x in ("Chronic calculous cholecystitis", "Stage 3 CKD"))
    facts = [{"type": "diagnosis", "key": k, "label": k, "assertion": "present", "clinical_time": "2026-10-20T10:00:00+05:30",
              "value": {"text": k}, "id": f"f{i}", "status": "confirmed"} for i, k in enumerate(("NYHA 4", "Dilated cardiomyopathy"))]
    assert t._diagnosis_lines(facts)[0] == "Dilated cardiomyopathy"
    sugar = lambda text, n, unit: {"type": "lab_result", "key": "blood glucose", "value": {"text": text, "number": n, "unit": unit}}
    assert t.outside(sugar("9.3", 9.3, None))[0] == "high"                     # 9.3 mmol/L is high, not low
    assert t.outside(sugar("9.3 mg/dl", 9.3, "mg/dl"))[0] is None              # not claimed LOW; the value stays as written
    assert t.outside(sugar("60", 60, "mg/dL"))[0] == "low" and t.outside(sugar("90", 90, "mg/dL"))[0] is None


def test_owner_rule_a_value_outside_the_normal_range_is_an_alert():
    page = _page([fact("lab_result", "sodium", "133", "2026-10-20", 133), fact("lab_result", "potassium", "4.1", "2026-10-20", 4.1)])
    assert [a["title"] for a in page["alerts"]] == ["Sodium low"]          # 133 is under 135: an alert; potassium 4.1 is normal
