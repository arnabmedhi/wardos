"""Offline test: hospital lab-report lines -> lab_result facts by code (labpdf.py), with a stand-in parser."""
from __future__ import annotations

from wardos.chain import labpdf
from wardos.record.labs import LabValue

PAGE = ("Lab Center Name : Biochemistry Lab Sample No : BIO1\nTest Name Result Unit Reference\n"
        "Serum Creatinine 1.4 H mg/dl 0.67 - 1.17\nPT/INR\nRemark: sample haemolysed, repeat 2 ml")


def _value(analyte, value, kind="value", **kw):
    return LabValue(analyte=analyte, value=value, number=kw.get("number"), flag=kw.get("flag"), unit=kw.get("unit"),
                    reference=None, panel=None, lab="Biochemistry Lab", sample_no="BIO1",
                    collected_at="2026-10-21T08:30:00", verified_at=None, page=1, source_sha256="x", kind=kind)


def test_lab_lines_become_dated_facts_headings_skipped(monkeypatch):
    values = [_value("Serum Creatinine", "1.4", number=1.4, flag="H", unit="mg/dl"),
              _value("note", "PT/INR", kind="note"),
              _value("note", "Remark: sample haemolysed, repeat 2 ml", kind="note")]
    monkeypatch.setattr("wardos.record.labs.parse_lis_pdf", lambda path, expected_registration=None: (values, []))
    pages = [{"id": "pg1", "number": 1, "text": PAGE}]
    cands, warnings = labpdf.lab_candidates(b"%PDF", pages, admission={"id": "a"}, source_id="s")
    assert [c["type"] for c in cands] == ["lab_result", "investigation_result"]
    lab = cands[0]
    assert lab["value"]["text"] == "1.4 H mg/dl" and lab["value"]["sample_no"] == "BIO1"
    assert lab["clinical_time"] == "2026-10-21T08:30:00" and lab["time_precision"] == "exact"
    assert all(c["quote_found"] for c in cands) and not warnings
    assert labpdf.is_lab_report([PAGE]) and not labpdf.is_lab_report(["Discharge advice: tab PCM SOS"])
