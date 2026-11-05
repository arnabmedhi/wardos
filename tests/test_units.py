"""Offline unit tests: no browser, no network, no model."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from fake_portal import make_pdf
from wardos.hospital.firewall import WriteFirewall, mask_path
from wardos.hospital.otp import OtpRequest, parse_otp_email
from wardos.hospital.pdfcheck import check_pdf
from wardos.hospital.runner import render
from wardos.hospital.skills import SkillError, SkillLibrary, parse_date, resolve_inputs, validate

SENDER, SUBJECT, INBOX = "noreply@ehospital.example", "OTP for Login", "me@example.com"


def _email(body: str, *, sender=SENDER, subject=SUBJECT, to=INBOX, sent: datetime | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"] = format_datetime(sent or datetime.now(timezone.utc))
    msg.set_content(body)
    return bytes(msg)


def _request(offset_s=0) -> OtpRequest:
    return OtpRequest("1", 10, datetime.now(timezone.utc) - timedelta(seconds=offset_s), 150)


def test_otp_accepts_one_fresh_code():
    request = _request(5)
    raw = _email("Your OTP for login is 482913. Valid for 2 minutes.")
    assert parse_otp_email(raw, sender=SENDER, subject=SUBJECT, recipient=INBOX, request=request,
                           received_at=datetime.now(timezone.utc)) == "482913"


@pytest.mark.parametrize("kwargs, body", [
    ({"sender": "other@x.com"}, "Your OTP is 482913"),
    ({"subject": "Newsletter"}, "Your OTP is 482913"),
    ({"to": "someone@else.com"}, "Your OTP is 482913"),
    ({}, "Your OTP is 482913 or 123456"),
    ({}, "Meeting at 482913"),
    ({"sent": datetime.now(timezone.utc) - timedelta(minutes=10)}, "Your OTP is 482913"),
])
def test_otp_rejects_wrong_or_ambiguous_mail(kwargs, body):
    raw = _email(body, **kwargs)
    assert parse_otp_email(raw, sender=SENDER, subject=SUBJECT, recipient=INBOX, request=_request(5),
                           received_at=datetime.now(timezone.utc)) is None


def test_otp_rejects_mail_received_outside_window():
    request = _request(500)
    assert parse_otp_email(_email("Your OTP is 482913"), sender=SENDER, subject=SUBJECT, recipient=INBOX,
                           request=request, received_at=datetime.now(timezone.utc)) is None


def test_firewall_decisions():
    fw = WriteFirewall(mode="enforce", allow=[r"^/api/authentication/", r"^/api/lis/report/search$"], hosts=["h.test"])
    assert fw.decide("GET", "https://h.test/api/anything") == "pass"
    assert fw.decide("POST", "https://h.test/api/authentication/v1/login") == "allowed"
    assert fw.decide("POST", "https://h.test/api/lis/report/search") == "allowed"
    assert fw.decide("POST", "https://h.test/api/lis/remarks/save") == "blocked"
    assert fw.decide("POST", "https://other.test/api/save") == "pass"
    with fw.allow_once(r"^/api/lis/remarks/save$"):
        assert fw.decide("POST", "https://h.test/api/lis/remarks/save") == "allowed_once"
        assert fw.decide("POST", "https://h.test/api/lis/remarks/save") == "blocked"
    observe = WriteFirewall(mode="observe")
    assert observe.decide("DELETE", "https://h.test/api/x") == "observed"
    assert mask_path("/api/patient/20260000001/report") == "/api/patient/{n}/report"


def test_shipped_skills_are_valid():
    library = SkillLibrary()
    names = {s["skill"] for s in library.list("ehospital")}
    assert {"nav.lis_report", "lis.fetch_lab_reports"} <= names


def test_skill_validation_catches_mistakes():
    with pytest.raises(SkillError):
        validate({"skill": "x", "portal": "p", "version": 1, "kind": "read",
                  "steps": [{"id": "a", "action": "click"}]})
    with pytest.raises(SkillError):
        validate({"skill": "x", "portal": "p", "version": 1, "kind": "read",
                  "steps": [{"id": "a", "action": "teleport"}]})


def test_inputs_defaults_dates_and_templates():
    skill = SkillLibrary().load("ehospital", "lis.fetch_lab_reports")
    values = resolve_inputs(skill, {"uhid": " 20260000001 "})
    assert values["uhid"] == "20260000001"
    assert values["select"] == "new"
    dated = resolve_inputs({"inputs": {"d": {"type": "date", "default": "today-120d"}}}, {})
    assert dated["d"] == date.today() - timedelta(days=120)
    assert render("{from_date:%d/%m/%Y}", {"from_date": date(2026, 10, 5)}) == "05/10/2026"
    assert parse_date("today+2d") == date.today() + timedelta(days=2)
    with pytest.raises(SkillError):
        resolve_inputs(skill, {})
    with pytest.raises(SkillError):
        resolve_inputs(skill, {"uhid": "1", "select": "everything"})


def test_lab_line_parser_handles_flags_missing_units_and_notes():
    from wardos.record.labs import parse_result_line
    assert parse_result_line("Serum Creatinine 0.75 mg/dl 0.67 - 1.17") == {
        "analyte": "Serum Creatinine", "value": "0.75", "number": 0.75, "flag": None, "unit": "mg/dl", "reference": "0.67 - 1.17"}
    row = parse_result_line("Direct Bilirubin 0.22 H mg/dl 0.1 - 0.2")
    assert row["flag"] == "H" and row["unit"] == "mg/dl" and row["number"] == 0.22
    row = parse_result_line("SERUM NA+ 135 L mmol/L 136 - 146")
    assert row["analyte"] == "SERUM NA+" and row["flag"] == "L"
    row = parse_result_line("AMYLASE 35 - 28 - 100")
    assert row["unit"] is None and row["reference"] == "28 - 100"
    assert parse_result_line("(Lab no.-960)") is None
    assert parse_result_line("Kidney Function Test") is None


def test_lab_record_latest_is_by_collection_time(tmp_path):
    from wardos.record.labs import LabRecord, LabValue, answer_latest
    record = LabRecord(tmp_path, "p", "x")
    def value(v, collected, sample):
        return LabValue("Serum Creatinine", v, float(v), None, "mg/dl", "0.67 - 1.17", "KFT", "Biochemistry Lab",
                        sample, collected, None, 1, "sha")
    record.add([value("1.4", "2026-10-22T08:30:00", "S2"), value("1.6", "2026-10-21T08:00:00", "S1")], tmp_path / "a.pdf")
    record.add([value("1.7", "2026-10-20T07:00:00", "S0")], tmp_path / "b.pdf")
    text = answer_latest(record, "creatinine", checked_at="now", new_since="2000-01-01")
    assert text.splitlines()[0].startswith("Serum Creatinine: 1.4")
    assert "previous: 1.6" in text


def test_pdf_check(tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(make_pdf(["Patient UHID: 20260000001", "Haemoglobin 12.5 g/dL 13.0 - 17.0",
                               "Creatinine 1.1 mg/dL 0.7 - 1.3", "Sodium 138 mmol/L 135 - 145"]))
    result = check_pdf(good, "20260000001")
    assert result.ok, result.reasons
    assert not check_pdf(good, "2026000000").identifier_found
    inventory = tmp_path / "inventory.pdf"
    inventory.write_bytes(make_pdf(["Patient UHID: 20260000001", "Tests performed:", "CBC", "LFT", "KFT"]))
    assert not check_pdf(inventory, "20260000001").ok


def test_survey_menu_tree_takes_menus_not_user_records():
    from wardos.hospital.survey import menu_tree, tree_lines
    payload = {"user": {"name": "Dr Someone", "email": "x@y"},
               "data": [{"menuName": "LIS Transaction", "children": [
                   {"menuName": "Report", "url": "/lis/report"},
                   {"menuName": "Order Status", "url": "/lis/order/12345"}]},
                        {"menuName": "IPD", "subMenus": [{"menuName": "Bed Status", "routerLink": "/ipd/beds"}]}]}
    tree = menu_tree(payload)
    assert [n["label"] for n in tree] == ["LIS Transaction", "IPD"]
    assert tree[0]["children"][1]["url"] == "/lis/order/{n}"
    assert not any("Someone" in line for line in tree_lines(tree))


def test_firewall_read_like_posts():
    from wardos.hospital.firewall import read_like
    fw = WriteFirewall(mode="enforce", allow=[r"^/api/authentication/"], hosts=["h.test"], allow_read_like=True)
    assert fw.decide("POST", "https://h.test/api/ipd/common/wardbyDeptArray") == "allowed_read"
    assert fw.decide("POST", "https://h.test/api/ipd/patient/getByUhid") == "allowed_read"
    assert fw.decide("POST", "https://h.test/api/lis/report/search") == "allowed_read"
    for write in ("/api/ipd/discharge/save", "/api/ipd/bed/allotBed", "/api/ipd/transfer/getTransferSave",
                  "/api/ipd/discharge/getList", "/api/ipd/patient/update", "/api/lis/sample/verifyResult"):
        assert fw.decide("POST", "https://h.test" + write) == "blocked", write
    assert not read_like("/api/ipd/admit")
    strict = WriteFirewall(mode="enforce", hosts=["h.test"])
    assert strict.decide("POST", "https://h.test/api/ipd/common/wardbyDeptArray") == "blocked"


def test_explore_guard_and_decision_parsing():
    from wardos.hospital.explore import is_blocked, parse_decision, safe_label, value_ref
    assert is_blocked({"tag": "button", "label": "Save", "zone": "main"})
    assert is_blocked({"tag": "button", "label": "Discharge Patient", "zone": "dialog"})
    assert is_blocked({"tag": "a", "label": "Logout", "zone": "header"})
    assert is_blocked({"tag": "a", "label": "Discharge Initiation", "zone": "menu"}) is None
    assert is_blocked({"tag": "button", "label": "Search", "zone": "main"}) is None
    assert safe_label({"label": "RAM KUMAR", "table_column": "Patient Name", "table_row": 2}) == "table row 2, column 'Patient Name'"
    assert parse_decision({"action": "click", "element": "7"})["element"] == 7
    with pytest.raises(ValueError):
        parse_decision({"action": "click"})
    with pytest.raises(ValueError):
        parse_decision({"action": "teleport"})
    assert value_ref("20260000001", {"uhid": "20260000001"}) == "{uhid}"
    assert value_ref("20260000001", {}) == "(not recorded)"


def test_learn_condense_and_finalize():
    from wardos.hospital.learn import condense_trace, finalize, parse_input_spec
    cell = {"tag": "input", "id": "piscine", "name": "piscine", "type": "checkbox", "css": "input#piscine",
            "in_table": True, "column": "Action", "path": "/lis/report"}
    events = [{"kind": "click", "tag": "input", "name": "uhid", "css": "form > input", "path": "/lis/report"},
              {"kind": "typed", "tag": "input", "name": "uhid", "css": "form > input", "path": "/lis/report", "value": "{uhid}"},
              {"kind": "click", "tag": "button", "text": "Search", "css": "form > button", "path": "/lis/report"},
              {"kind": "click", **cell}, {"kind": "toggle", **cell, "checked": True},
              {"kind": "click", **cell}, {"kind": "toggle", **cell, "checked": True}]
    lines, start = condense_trace(events)
    assert start == "/lis/report" and len(lines) == 3
    assert "value={uhid}" in lines[0] and lines[2].endswith("x2")
    inputs = parse_input_spec(["uhid:string:required", "select:string:default=new:choices=new|all"])
    assert inputs["select"] == {"type": "string", "default": "new", "choices": ["new", "all"]}
    skill = finalize({"steps": [{"id": "a", "action": "fill", "target": {"css": "x"}, "value": "{uhid}"}]},
                     name="t.x", portal="p", inputs=inputs, source="test")
    assert skill["kind"] == "read" and skill["version"] == 1
    with pytest.raises(SkillError):
        finalize({"steps": [{"id": "a", "action": "fill", "target": {"css": "x"}, "value": "{ward}"}]},
                 name="t.x", portal="p", inputs=inputs, source="test")


def test_date_boxes_are_read_back_like_a_person_would():
    from datetime import datetime
    from wardos.hospital.dates import on_page, parse_value, shows
    when = parse_value("22/10/2026 23:59:59")
    assert when == datetime(2026, 10, 22, 23, 59, 59) and parse_value("22/10/2026") == datetime(2026, 10, 22)
    assert shows("22/10/2026, 23:59:00", when) and shows("2026-10-22", when)
    assert not shows("22/10/2026, 00:00:00", when)
    assert not shows("23/10/2026, 23:59:59", when) and not shows("", when)
    assert on_page("Patient admitted during the reporting period of 22-10-2026 00:00:00 to 22-10-2026 23:59:59", when)
    assert not on_page("reporting period of 23-10-2026 00:00:00 to 23-10-2026 20:18:00", when)
    assert on_page("22 Oct 2026", when) and not on_page("122-10-20260", when)


def test_typed_dates_and_calendar_clicks_in_a_recording():
    from wardos.hospital.explore import value_ref
    from wardos.hospital.learn import draft_from_recording
    dates = {"from_date": "22/10/2026 00:00:00", "to_date": "22/10/2026 23:59:59"}
    assert value_ref("22/10/2026, 00:00:00", dates) == "{from_date}"
    assert value_ref("22/10/2026", dates) == "{from_date|to_date}"
    box = {"kind": "typed", "tag": "input", "fcn": "endDate", "css": "input[formcontrolname='endDate']",
           "path": "/r", "value": "{from_date|to_date}"}
    day = {"kind": "click", "tag": "button", "aria": "22 October 2026", "css": "mat-calendar td > button", "path": "/r"}
    arrow = {"kind": "click", "tag": "button", "aria": "Previous month", "css": "mat-calendar-header > button", "path": "/r"}
    steps = draft_from_recording([box, arrow, day], dates)
    assert [s["action"] for s in steps] == ["home", "fill"]
    assert steps[1]["value"] == "{to_date}"


def test_layered_library_prefers_staging(tmp_path):
    import json as _json
    from wardos.config import SKILLS_DIR
    from wardos.hospital.skills import LayeredSkillLibrary
    staged = tmp_path / "ehospital"
    staged.mkdir()
    (staged / "x.new.json").write_text(_json.dumps({"skill": "x.new", "portal": "ehospital", "version": 1, "kind": "read",
                                                    "steps": [{"id": "n", "action": "call_skill", "skill": "nav.lis_report"}]}))
    library = LayeredSkillLibrary([tmp_path, SKILLS_DIR])
    assert library.load("ehospital", "x.new")["version"] == 1
    assert library.load("ehospital", "nav.lis_report")["skill"] == "nav.lis_report"
    assert {"x.new", "nav.lis_report"} <= {s["skill"] for s in library.list("ehospital")}


def test_explore_map_text():
    from wardos.hospital.explore import map_text
    catalog = {"modules": [{"tile": "IPD", "links": [
        {"text": "Bed Allocation", "group": "Admission/Discharge/Transfer", "zone": "menu"},
        {"text": "Bed Occupancy", "group": "Report", "zone": "menu"},
        {"text": "Home", "group": "", "zone": "header"}]}, {"tile": "Empty", "links": []}]}
    text = map_text(catalog)
    assert "IPD -> Admission/Discharge/Transfer: Bed Allocation | Report: Bed Occupancy" in text
    assert "Home" not in text and "Empty" not in text
