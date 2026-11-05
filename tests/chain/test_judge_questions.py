"""The questions a visitor will try, routed with no model."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wardos.chain import ask as asking
from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.register import lookup_requested


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    record = Record(SQLiteDocStore(tmp_path_factory.mktemp("j") / "r.sqlite3"), LocalFileStore(tmp_path_factory.mktemp("f")))
    ids = {}
    for bed, name, labs in (("12", "Ravi Wardos", {"Potassium": ("3.1", 3.1), "Hb": ("9", 9.0), "Sodium": ("140", 140.0)}),
                            ("13", "Sunil Wardos", {"Potassium": ("6.9", 6.9), "Creatinine": ("0.5", 0.5)})):
        patient, admission = record.create_stay(name=name, uhid=f"2026{bed}", unit="Medicine", bed=bed)
        source, _ = record.add_source(admission["id"], "text", text="x", mime="text/plain", author="dr")
        for i, (label, (text, number)) in enumerate(labs.items()):
            key = asking.fact_key("lab_result", label)
            record.store.put(m.FACTS, m.fact_doc(admission_id=admission["id"], source_id=source["id"], page_id="p", index=i,
                                                 type="lab_result", key=key, label=label, value={"text": text, "number": number},
                                                 clinical_time="2026-10-11", time_precision="day", assertion="present",
                                                 quote=text, quote_found=True) | {"id": f"f{bed}{i}", "added_in": 1})
        ids[bed] = admission["id"]
    return record, ids


def ask(world, words, bed=None):
    record, ids = world
    return asking.ask(record, words, ids[bed] if bed else None)


@pytest.mark.parametrize("words", ["is Sunil's creatinine getting worse", "what was the potassium of bed 12 on 15 oct",
                                   "how old is Sunil", "is bed 12's potassium low", "is bed 13 getting better"])
def test_a_real_question_goes_to_ask_anything(world, words):
    assert ask(world, words)["card"]["kind"] == "ai"


@pytest.mark.parametrize("words,title,bed", [
    ("Hb of bed 12", "Hb", "12"), ("potasium bed 12", "Potassium", "12"), ("creatnine of bed 13", "Creatinine", "13"),
    ("bed twelve potassium", "Potassium", "12"), ("K of bed 12", "Potassium", "12"), ("Na of bed 12", "Sodium", "12"),
    ("Bed Thirteen creatinine", "Creatinine", "13")])
def test_one_value_is_shown_alone(world, words, title, bed):
    card = ask(world, words)["card"]
    assert card["kind"] == "tests" and card["filtered"] and card["title"] == title and card["patient"]["bed"] == bed


def test_which_values_are_abnormal_is_the_screens_own_list(world):
    card = ask(world, "which values are abnormal in bed 13")["card"]
    assert card["kind"] == "tests" and not card["filtered"] and "Potassium" in [r["label"] for r in card["abnormal"]]


@pytest.mark.parametrize("words", ["which patients are sick", "who is the sickest", "show all patients", "how many patients"])
def test_the_ward_question_names_every_patient(world, words):
    card = ask(world, words)["card"]
    assert card["kind"] == "message" and "Bed 12" in card["text"] and "Bed 13" in card["text"]


def test_a_ward_question_on_one_test_gives_each_newest_value(world):
    text = ask(world, "which patient has the highest potassium")["card"]["text"]
    assert "Bed 12 · Ravi: 3.1" in text and "Bed 13 · Sunil: 6.9" in text


def test_any_alerts_on_an_open_page_stays_on_that_patient(world):
    assert ask(world, "any alerts", "12")["card"]["kind"] == "ai"


@pytest.mark.parametrize("words,bed", [("Rahul's potassium", None), ("Rahul's potassium", "12"), ("rahul ka potassium", "12")])
def test_a_name_not_on_the_ward_never_shows_another_patients_value(world, words, bed):
    card = ask(world, words, bed)["card"]
    assert card["kind"] == "message" and "no patient called Rahul" in card["text"]


@pytest.mark.parametrize("words,bed,kind", [("Ravi's potassium", None, "tests"), ("today's potassium", "12", "tests"),
                                            ("sunil ka report dikhao", None, "message"), ("patient's medicines", "13", "message")])
def test_ordinary_possessives_are_not_strangers(world, words, bed, kind):
    card = ask(world, words, bed)["card"]
    assert card["kind"] in (kind, "reports") and "no patient called" not in (card.get("text") or "")


def test_normal_range_comes_from_the_table(world):
    assert "3.5 to 5 mmol/L" in ask(world, "what is the normal range of potassium")["card"]["text"]
    assert "4 to 11 K/µL" in ask(world, "normal range of tlc")["card"]["text"]


def test_a_treatment_question_is_refused_plainly(world):
    assert ask(world, "what should I give for hyperkalemia")["card"]["text"] == asking.NO_ADVICE


def test_hello_gets_examples(world):
    assert "lowest potassium for bed 12" in ask(world, "hello")["card"]["text"]


def test_how_old_is_not_an_old_patient_lookup():
    assert not lookup_requested("how old is Sunil")
    assert lookup_requested("old patient Sunil")


def test_test_words_change_only_what_they_should():
    assert asking.test_words("vitamin k of bed 12") == "vitamin k of bed 12"
    assert asking.test_words("potasium") == "potassium"
    assert asking.test_words("harish medicine") == "harish medicine"


def test_the_list_has_at_least_forty_and_every_group():
    rows = json.loads((Path(__file__).parent / "judge_questions.json").read_text(encoding="utf-8"))
    assert len(rows) >= 40 and {"values", "record", "command", "photo", "odd"} <= {r["group"] for r in rows}
    assert len({r["n"] for r in rows}) == len(rows)
