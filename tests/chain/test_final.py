"""Offline tests for the input handed to the summary service for the final summary (final.py)."""
from __future__ import annotations

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.final import final_reading, ordered_pages
from wardos.chain.record import Record


def _page(rec, adm, text, when, date=None, held=False):
    src, _ = rec.add_source(adm["id"], "photo", data=text.encode(), mime="image/jpeg", received_at=when)
    (page,) = rec.save_pages(src["id"], [{"number": 1, "text": text}])
    rec.update_pages({page["id"]: {"page_date": date, "held": held}})
    return page


def test_pages_in_clinical_order_held_left_out_corrections_last(tmp_path):
    rec = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    adm = rec.create_admission(rec.create_patient(name="Test")["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")
    _page(rec, adm, "day two note", "2026-10-22T09:00:00", "2026-10-21")
    _page(rec, adm, "undated continuation", "2026-10-22T09:01:00")
    _page(rec, adm, "admission note", "2026-10-22T09:02:00", "2026-10-20")
    _page(rec, adm, "someone else", "2026-10-22T09:03:00", "2026-10-20", held=True)
    assert [p["text"] for p in ordered_pages(rec, adm["id"])] == ["admission note", "day two note",
                                                                   "undated continuation"]
    fact = m.fact_doc(admission_id=adm["id"], source_id="s", page_id="p", index=0, type="vital", key="pulse",
                      label="Pulse", value={"text": "88/min"}, clinical_time="2026-10-20", time_precision="day",
                      assertion="present", quote="q", quote_found=True)
    rec.store.put(m.FACTS, fact)
    rec.correct_fact(fact["id"], doctor="dr", text="90/min")
    text = final_reading(rec, adm["id"])
    assert text.startswith("**Image 1:**\n\nadmission note") and "**Image 3:**\n\nundated continuation" in text
    assert "someone else" not in text and text.rstrip().endswith("- Pulse: 90/min (2026-10-20)")
