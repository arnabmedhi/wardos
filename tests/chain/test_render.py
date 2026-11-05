"""The summary writer: code-built lab table, rewrite only what changed, doctor edits kept."""
from __future__ import annotations

import json
import re

import pytest

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import check_iso_time, fact_key
from wardos.chain.record import Record
from wardos.chain.render import DOCUMENTS, edit_field, lab_table, summary_text, update_summary
from wardos.chain.units import feeds, load_unit
from wardos.models import Usage


class FakeWriter:

    def __init__(self):
        self.calls = []

    async def generate(self, prompt, **kwargs):
        fields = re.findall(r"^### (\{\{\w+\}\})", prompt, re.M)
        facts = re.findall(r"^\[(F\d+)\] (\w+) \| [^|]+ \| ([^|]+) \|", prompt, re.M)
        self.calls.append(fields)
        out = {}
        for field in fields:
            out[field] = {"text": "; ".join(t.strip() for _, _, t in facts), "facts": [f for f, _, _ in facts]}
        return {"fields": out}, Usage("fake", cost_inr=0.0)


@pytest.fixture
def setup(tmp_path):
    rec = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    patient = rec.create_patient(name="Test Patient")
    adm = rec.create_admission(patient["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")
    return rec, adm


def add(rec, adm, tag, facts):
    src, _ = rec.add_source(adm["id"], "text", text=tag)
    docs = []
    for i, (type_, key, text, when) in enumerate(facts):
        t, p = check_iso_time(when) if when else (None, "unknown")
        docs.append(m.fact_doc(admission_id=adm["id"], source_id=src["id"], page_id=None, index=i, type=type_,
                               key=fact_key(type_, key), label=key, value={"text": text}, clinical_time=t,
                               time_precision=p, assertion="present", quote=text, quote_found=True))
    rec.apply(src["id"], docs)


def test_lab_table_is_built_by_code(setup):
    rec, adm = setup
    add(rec, adm, "labs", [("lab_result", "Hb", "9.8", "2026-10-20"), ("lab_result", "Hb", "10.2", "2026-10-22"),
                           ("lab_result", "creatinine", "0.9", "2026-10-20")])
    table = lab_table(rec.facts(adm["id"]))
    assert table.splitlines()[0] == "Test | 20/10 | 22/10"
    assert "Hb | 9.8 | 10.2" in table and "creatinine | 0.9 | -" in table


def test_field_feeds():
    assert feeds("{{pulse}}") >= {"vital"} and "lab_result" not in feeds("{{pulse}}")
    assert "diagnosis" in feeds("{{final_diagnosis}}") and "med_event" in feeds("{{discharge_advice}}")
    unit = load_unit("Surgery Unit 2")
    assert any(f["placeholder"] == "{{labs_json}}" and f["by_code"] for f in unit["fields"])


async def test_only_changed_fields_are_rewritten_and_doctor_edits_stay(setup):
    rec, adm = setup
    writer = FakeWriter()
    add(rec, adm, "day1", [("vital", "pulse", "88/min", "2026-10-20"), ("diagnosis", "cholelithiasis",
                                                                        "Cholelithiasis", "2026-10-20")])
    first = await update_summary(rec, writer, adm["id"], model_id="fake")
    assert first["written"] >= 2
    again = await update_summary(rec, writer, adm["id"], model_id="fake")
    assert again["written"] == 0

    edit_field(rec, adm["id"], "{{pulse}}", "PR 88/min, regular", doctor="dr.a")
    add(rec, adm, "day2", [("vital", "pulse", "92/min", "2026-10-21"),
                           ("diagnosis", "acute cholecystitis", "Acute calculous cholecystitis", "2026-10-21")])
    third = await update_summary(rec, writer, adm["id"], model_id="fake")
    doc = rec.store.get(DOCUMENTS, m.stable_id("doc", adm["id"], "discharge"))
    assert doc["fields"]["{{pulse}}"]["doctor_text"] == "PR 88/min, regular"
    assert doc["fields"]["{{pulse}}"]["suggestion"] is True
    assert "Acute calculous cholecystitis" in doc["fields"]["{{final_diagnosis}}"]["text"]
    assert third["suggestions"] >= 1
    assert "PR 88/min, regular" in summary_text(rec, adm["id"])
    rewritten = {f for call in writer.calls[1:] for f in call}
    assert "{{pulse}}" not in rewritten and "{{final_diagnosis}}" in rewritten


async def test_cited_ids_outside_the_field_are_dropped(setup):
    rec, adm = setup

    class Liar(FakeWriter):
        async def generate(self, prompt, **kwargs):
            fields = re.findall(r"^### (\{\{\w+\}\})", prompt, re.M)
            return {"fields": {f: {"text": "x", "facts": ["F999"]} for f in fields}}, Usage("fake", cost_inr=0.0)
    add(rec, adm, "d", [("vital", "pulse", "88/min", "2026-10-20")])
    await update_summary(rec, Liar(), adm["id"], model_id="fake")
    doc = rec.store.get(DOCUMENTS, m.stable_id("doc", adm["id"], "discharge"))
    assert doc["fields"]["{{pulse}}"]["fact_ids"] == [] and doc["fields"]["{{pulse}}"]["dropped_ids"] == 1

