"""What may go on the demo OpenMRS, and reading its Results screen."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import openmrs_seed as seed  # noqa: E402
import openmrs_watch as watch  # noqa: E402

LIVE = ("active", "doctor_locked")


def fact(key, text, number, when, **extra):
    return {"type": "lab_result", "status": "active", "assertion": "present", "time_precision": "exact", "key": key,
            "clinical_time": when, "label": "SECRET LABEL", "quote": "SECRET QUOTE", "source_id": "SECRET",
            "value": {"number": number, "text": text, "sample_no": "SECRET", "lab": "SECRET"}, **extra}


def reports(facts):
    return seed.reports_from_facts(facts, LIVE, lambda f: f["value"]["number"])


def test_only_test_value_unit_and_gaps_leave_the_record():
    out, dropped = reports([fact("potassium", "4.3", 4.3, "2026-07-06T21:45:07"),
                            fact("potassium", "6.1", 6.1, "2026-07-08T21:45:07")])
    assert dropped == 0
    assert [r["offset_s"] for r in out] == [0, 2 * 86400]
    text = str(out)
    assert "SECRET" not in text and "2026" not in text
    assert set(out[0]["results"][0]) == {"key", "concept", "value", "unit"}


def test_unusable_values_do_not_go_public():
    out, dropped = reports([
        fact("potassium", "4.3", 4.3, "2026-07-06T10:00:00"),
        fact("urea", "H 426", 426.0, "2026-07-06T10:00:00", quote_found=False),
        fact("potassium", "43", 43.0, "2026-07-06T10:00:00"),
        fact("sodium", "135", 135.0, "2026-07-06T10:00:00", status="superseded"),
        fact("sodium", "135", 135.0, "2026-07-06T10:00:00", assertion="absent"),
        fact("sodium", "135", 135.0, "unknown", time_precision="unknown"),
        fact("some other test", "1", 1.0, "2026-07-06T10:00:00"),
    ])
    assert [x["key"] for x in out[0]["results"]] == ["potassium"]
    assert dropped == 2


def test_units_are_changed_to_the_sites():
    assert seed.site_value("tlc", 13400) == 13.4
    assert seed.site_value("platelets", 137000) == 137
    assert seed.site_value("creatinine", 1.0) == 88.4
    assert seed.site_value("bilirubin", 1.0) == 17.1
    assert seed.in_record_unit("bilirubin", 27.0, {"value": {"text": "27 umol/L"}}) == 27.0 / 17.1
    assert seed.site_value("sodium", 138.4) == 138 and isinstance(seed.site_value("sodium", 138.4), int)
    assert seed.site_value("potassium", 43) is None


def test_the_last_report_is_held_back():
    kept, held = seed.split_held_back([{"offset_s": 0}, {"offset_s": 9}])
    assert kept == [{"offset_s": 0}] and held == {"offset_s": 9}


def test_running_again_adds_nothing_and_later_reports_are_found():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    person = {"reports": [{"offset_s": 0, "results": ["a"]}, {"offset_s": 3600, "results": ["b"]}]}
    first = start + timedelta(hours=1)
    assert len(seed.missing_reports(person, start, [])) == 2
    there = [{"encounterDatetime": seed.iso(first + timedelta(seconds=s))} for s in (0, 3600)]
    assert seed.missing_reports(person, start, there) == []
    assert seed.later_than_kept(person, start, there) == []
    later = {"encounterDatetime": seed.iso(first + timedelta(days=2))}
    assert seed.later_than_kept(person, start, there + [later]) == [later]


def test_luhn_number_matches_the_site_format():
    assert seed.luhn_number(7992739871) == "7992739871-3"


def rows(*items):
    return [{"cells": {"Test name": n, "Result date": d, "Value": v, "Reference range": r}} for n, d, v, r in items]


NOW = datetime(2026, 10, 30, 15, 55)


def test_screen_rows_become_values():
    out, unread = watch.values_from_rows(rows(
        ("Serum potassium", "28-Oct-2026,\n02:19 PM", "2.6 mmol/L\n↓", "3.5 – 5.6 mmol/L"),
        ("Serum calcium", "27-Oct-2026, 02:19 PM", "8.7 mg/dL", "-- mg/dL"),
        ("Some test", "27-Oct-2026, 02:19 PM", "Positive", "--")), NOW)
    assert unread == 0
    assert [(v.analyte, v.number, v.unit) for v in out] == [("Serum potassium", 2.6, "mmol/L"), ("Serum calcium", 8.7, "mg/dL")]
    assert out[0].collected_at == "2026-10-28T14:19:00" and out[0].reference == "3.5 – 5.6 mmol/L"
    assert out[1].reference is None


def test_newest_results_written_as_today_are_read():
    out, unread = watch.values_from_rows(rows(("Serum potassium", "Today, 03:50 PM", "6.8 mmol/L", "--"),
                                              ("Serum sodium", "Yesterday, 11:05 AM", "138 mmol/L", "--")), NOW)
    assert unread == 0
    assert [v.collected_at for v in out] == ["2026-10-30T15:50:00", "2026-10-29T11:05:00"]


def test_a_number_with_an_unreadable_date_is_counted_not_hidden():
    out, unread = watch.values_from_rows(rows(("Serum potassium", "just now", "6.8 mmol/L", "--")), NOW)
    assert out == [] and unread == 1


def test_only_unseen_values_are_new():
    old = {"analyte": "Serum potassium", "collected_at": "2026-10-28T14:19:00", "value": "2.6"}
    values, _ = watch.values_from_rows(rows(("Serum potassium", "28-Oct-2026, 02:19 PM", "2.6 mmol/L", "--"),
                                            ("Serum potassium", "Today, 03:40 PM", "6.8 mmol/L", "--")), NOW)
    assert [v.value for v in watch.new_values(values, [old])] == ["6.8"]


def test_danger_rule_on_a_screen_value():
    from wardos.hospital.watch import is_dangerous
    def row(value):
        return {"analyte": "Serum potassium", "number": value, "value": str(value), "unit": "mmol/L",
                "reference": "3.5 – 5.6 mmol/L"}
    assert is_dangerous(row(6.8)) and not is_dangerous(row(4.5)) and is_dangerous(row(2.9))    # rule: outside the normal range is an alert


def test_ward_counter_is_read_only_when_its_number_is_shown():
    from wardos.hospital.brain import page_stat
    page = "Ward 2\nPatients\n5\nFree beds\n0\nTotal beds\n5\n"
    assert page_stat(page, "Free beds") == 0 and page_stat(page, "Total beds") == 5
    assert page_stat("Free beds\n\nTotal beds\n5", "Free beds") is None
