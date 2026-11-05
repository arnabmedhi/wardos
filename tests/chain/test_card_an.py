"""Fixes from a phone test of the patient screen."""
from __future__ import annotations

import re

import pytest

from wardos.chain import model as m
from wardos.chain.ask import interpret, parse_new_patient
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.extract import REPLY_SCHEMA, extract_facts, reply_shape, to_candidates
from wardos.chain.files import LocalFileStore
from wardos.chain.normalize import split_flag
from wardos.chain.record import Record
from wardos.chain.settings import ChainSettings
from wardos.chain.today import _show, build_brief
from wardos.chain.worker import process_batch
from wardos.models import Usage, _first_json_object

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6, store="sqlite", files="local")
PAGE = {"p": 1, "type": "lab_report", "date": "2026-10-21", "patient": {"name": "Test Rina"}}
FACT = {"p": 1, "t": "lab_result", "k": "urea", "v": "40", "n": 40, "q": "Urea 40"}


@pytest.mark.parametrize("raw", [
    {"pages": [PAGE], "facts": [FACT]},
    {"value": [{"pages": [PAGE], "facts": [FACT]}]},
    {"value": [PAGE, FACT]},
    {"value": [{"pages": [PAGE]}, {"facts": [FACT]}]},
    {"data": {"pages": [PAGE], "facts": [FACT]}},
    {"result": {"pages": [PAGE], "items": [FACT]}},
    {"pages": [PAGE], "extracted_facts": [FACT]},
    {"pages": [{**PAGE, "facts": [{k: v for k, v in FACT.items() if k != "p"}]}]},
], ids=["proper", "list-of-one", "bare-list", "two-objects", "data", "items", "extracted", "inside-page"])
def test_near_shapes_keep_the_page_and_the_fact(raw):
    out = reply_shape(raw)
    assert [p["patient"]["name"] for p in out["pages"]] == ["Test Rina"]
    assert [(f["k"], f["p"]) for f in out["facts"]] == [("urea", 1)]


def test_a_real_bare_list_goes_through_the_json_helper():
    assert reply_shape(_first_json_object('[{"pages": [{"p": 1, "patient": {"name": "A B"}}], "facts": []}]'))["facts"] == []


@pytest.mark.parametrize("raw", [{"value": [FACT]}, {"facts": [FACT]}, {"facts": [FACT], "pages": []}],
                         ids=["bare-facts", "facts-only", "empty-pages"])
def test_facts_with_no_page_details_are_refused(raw):
    with pytest.raises(RuntimeError, match="no page details"):
        reply_shape(raw)


@pytest.mark.parametrize("raw", [{}, {"value": 5}, {"note": "sorry"}, {"value": []}])
def test_nothing_usable_is_an_error_that_names_only_the_shape(raw):
    with pytest.raises(RuntimeError, match="no facts list") as error:
        reply_shape(raw)
    assert "sorry" not in str(error.value)


async def test_the_fixed_shape_is_asked_for():
    seen = {}

    class Model:
        async def generate(self, prompt, **kwargs):
            seen.update(kwargs)
            return {"pages": [PAGE], "facts": []}, Usage("fake", cost_inr=0)
    await extract_facts(Model(), [{"number": 1, "text": "x"}], admitted=None, unit="", meds=[], model_id="fake")
    assert seen["schema"] is REPLY_SCHEMA and seen["as_json"] is True
    assert REPLY_SCHEMA["required"] == ["pages", "facts"]


class Fake:

    def __init__(self, bad=(), names=None):
        self.bad, self.names, self.calls = set(bad), names or {}, []

    async def generate(self, prompt, **kwargs):
        body = re.split(r"^=== PAGE (\d+) ===$", prompt, flags=re.M)[1:]
        self.calls.append(len(body) // 2)
        if any(word in prompt for word in self.bad):
            return {"note": "no"}, Usage("fake", cost_inr=0.01)
        pages, facts = [], []
        for number, chunk in zip(body[::2], body[1::2]):
            n, text = int(number), chunk.strip().splitlines()[0]
            pages.append({"p": n, "type": "lab_report", "date": "2026-10-21",
                          "patient": {"name": self.names.get(text, "Test Rina")}})
            facts.append({"p": n, "t": "lab_result", "k": f"test {text}", "v": "40", "n": 40, "w": "2026-10-21", "q": text})
        return {"pages": pages, "facts": facts}, Usage("fake", cost_inr=0.01)


@pytest.fixture
def world(tmp_path):
    rec = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    patient = rec.create_patient(name="Test Rina")
    adm = rec.create_admission(patient["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")
    ids = [rec.add_source(adm["id"], "photo", data=f"photo {i}".encode(), mime="image/jpeg")[0]["id"] for i in range(3)]
    return rec, adm, ids, {sid: f"page{i}" for i, sid in enumerate(ids)}


async def test_a_bad_batch_is_split_and_only_the_bad_page_waits(world):
    rec, adm, ids, saved = world
    model = Fake(bad=["page1"])
    report = await process_batch(rec, model, SETTINGS, adm["id"], ids, saved_text=saved)
    assert model.calls == [3, 3, 1, 1, 1, 1]
    assert [rec.source(s)["status"] for s in ids] == [m.SRC_MERGED, m.SRC_READ, m.SRC_MERGED]
    assert report.added == 2
    assert rec.source(ids[1])["not_added"] is True and not rec.source(ids[0]).get("not_added")


async def test_a_different_name_is_still_held_after_a_split(world):
    rec, adm, ids, saved = world
    report = await process_batch(rec, Fake(bad=["page1"], names={"page2": "Ramesh Chandra Joshi"}), SETTINGS, adm["id"],
                                 ids, saved_text=saved)
    assert report.held_pages == 1
    assert [q["kind"] for q in rec.questions(adm["id"])] == [m.Q_WRONG_PATIENT]


async def test_try_again_finishes_with_the_saved_reading(world):
    rec, adm, ids, saved = world
    await process_batch(rec, Fake(bad=["page1"]), SETTINGS, adm["id"], ids, saved_text=saved)
    report = await process_batch(rec, Fake(), SETTINGS, adm["id"], [ids[1]])
    assert rec.source(ids[1])["status"] == m.SRC_MERGED and report.pages_read == 0


def test_the_screen_stops_waiting_for_a_page_that_was_not_read(tmp_path):
    import asyncio
    from .test_api import _client
    from .test_beds import stay
    api = _client(tmp_path)
    rec = api.app.state.record
    aid = stay(api, "Test Rina", "5")["id"]
    sid = rec.add_source(aid, "photo", data=b"photo", mime="image/jpeg")[0]["id"]
    asyncio.run(process_batch(rec, Fake(bad=["page"]), SETTINGS, aid, [sid], saved_text={sid: "page0"}))
    assert api.get(f"/admissions/{aid}").json()["not_added"] == 1
    assert [s["id"] for s in api.get(f"/admissions/{aid}/today").json()["stuck"]] == [sid]
    assert api.post(f"/sources/{sid}/retry").status_code == 200
    assert not rec.source(sid).get("not_added")


@pytest.mark.parametrize("words", [
    "new patient bed 17 test Ram 40 male",
    "New patient, bed 17, test Ram, 40 male.",
    "naya patient bed 17 test Ram 40 male",
    "नया मरीज बेड 17 test Ram 40 पुरुष",
    "नया पेशेंट बेड १७ test Ram 40 मेल",
    "न्यू पेशेंट बेड 17 test Ram 40 मेल",
])
def test_spoken_new_patient_fills_every_box(words):
    assert interpret(words, [])["action"] == "new_patient"
    out = parse_new_patient(words, [])
    assert (out["bed"], out["name"], out["age"], out["sex"]) == ("17", "Test Ram", "40", "M")


@pytest.mark.parametrize("text", ["82 mmol/L", "82 mmol/ L", "82 mmol / L", "82 U/L", "82 g/L", "82 mg/L", "82 IU/L"])
def test_the_l_of_a_unit_is_not_a_low_flag(text):
    assert split_flag(text, 82) == (text, None)
    assert _show({"type": "lab_result", "value": {"text": text, "number": 82}}).endswith("/") is False


def test_a_value_saved_cut_shows_clean():
    assert _show({"type": "lab_result", "value": {"text": "82 mmol/", "number": 82, "flag": "L"}}) == "82"


@pytest.mark.parametrize("text,shown,flag", [("82 L", "82", "L"), ("82 (L)", "82", "L"), ("L 82", "82", "L"),
                                             ("H 426", "426", "H"), ("426 H", "426", "H")])
def test_a_real_flag_letter_still_comes_off(text, shown, flag):
    assert split_flag(text, float(shown)) == (shown, flag)


def test_a_new_page_keeps_its_unit_and_no_false_low():
    page = {"id": "pg", "source_id": "src", "text": "Chloride 150 mmol/L"}
    raw = {"facts": [{"p": 1, "t": "lab_result", "k": "chloride", "v": "150 mmol/L", "n": 150, "u": "mmol/L", "q": "Chloride 150 mmol/L"}]}
    value = to_candidates(raw, [page], admission={"id": "adm"})[0]["value"]
    assert value["text"] == "150 mmol/L" and "flag" not in value


def test_the_danger_line_carries_the_whole_unit():
    alerts = [{"name": "Chloride", "was": "91", "value": "82", "unit": "mmol/L", "go": "tests"},
              {"name": "Pulse", "was": "88", "value": "140", "unit": None, "go": "vitals"}]
    line = build_brief({"name": "Test"}, {"bed": "5"}, [], alerts, [])["lines"][2]
    assert line == "Dangerous: Chloride 91 → 82 mmol/L, Pulse 140"


def test_lab_name_loses_the_analyzer_hash_but_the_quote_keeps_it():
    page = {"id": "pg", "source_id": "src", "text": "4 Neu# &RH 11.98 10^3/uL"}
    raw = {"facts": [{"p": 1, "t": "lab_result", "k": "absolute neutrophil count", "l": "Neu#", "v": "11.98 10^3/uL",
                      "n": 11.98, "u": "10^3/uL", "q": "4 Neu# &RH 11.98 10^3/uL"}]}
    fact = to_candidates(raw, [page], admission={"id": "adm"})[0]
    assert fact["label"] == "Neu" and fact["quote"] == "4 Neu# &RH 11.98 10^3/uL" and fact["value"]["number"] == 11.98
