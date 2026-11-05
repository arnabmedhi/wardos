"""The batch worker: failures never lose facts and a retry finishes the job."""
from __future__ import annotations

import pytest

from wardos.chain import model as m
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import ChainSettings
from wardos.chain.worker import process_batch
from wardos.models import Usage

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6, store="sqlite", files="local")


class FakeExtractor:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    async def generate(self, prompt, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("model did not answer in time")
        pages = prompt.count("=== PAGE ")
        facts = [{"page": n, "type": "vital", "key": "pulse", "text": f"{80 + n}/min", "number": 80 + n,
                  "when": f"2026-10-2{n}", "assertion": "present", "quote": f"PR {80 + n}/min"}
                 for n in range(1, pages + 1)]
        info = [{"page": n, "page_type": "rounds_note", "date": f"2026-10-2{n}",
                 "patient": {"name": "Test Patient"}} for n in range(1, pages + 1)]
        return {"pages": info, "facts": facts}, Usage("fake", cost_inr=0.01)


@pytest.fixture
def setup(tmp_path):
    rec = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    patient = rec.create_patient(name="Test Patient")
    adm = rec.create_admission(patient["id"], unit="Surgery Unit 2", admitted_at="2026-10-20")
    ids = [rec.add_source(adm["id"], "photo", data=f"photo {i}".encode(), mime="image/jpeg")[0]["id"]
           for i in range(1, 4)]
    saved = {sid: f"Date 2{i}/10\nPR {80 + i}/min" for i, sid in enumerate(ids, 1)}
    return rec, adm, ids, saved


async def test_failed_extraction_leaves_sources_for_retry(setup):
    rec, adm, ids, saved = setup
    report = await process_batch(rec, FakeExtractor(fail_times=8), SETTINGS, adm["id"], ids, saved_text=saved)
    assert report.added == 0 and len(report.errors) >= 2
    assert {rec.source(s)["status"] for s in ids} == {m.SRC_READ}
    report = await process_batch(rec, FakeExtractor(), SETTINGS, adm["id"], ids, saved_text=saved)
    assert report.added == 3 and {rec.source(s)["status"] for s in ids} == {m.SRC_MERGED}


async def test_one_failure_is_retried_inside_the_batch(setup):
    rec, adm, ids, saved = setup
    report = await process_batch(rec, FakeExtractor(fail_times=1), SETTINGS, adm["id"], ids, saved_text=saved)
    assert report.added == 3 and report.quotes_found == 3


async def test_page_of_another_patient_is_held(setup):
    rec, adm, ids, saved = setup

    class OtherPatient(FakeExtractor):
        async def generate(self, prompt, **kwargs):
            raw, usage = await super().generate(prompt, **kwargs)
            raw["pages"][1]["patient"] = {"name": "Ramesh Chandra Joshi"}
            return raw, usage
    report = await process_batch(rec, OtherPatient(), SETTINGS, adm["id"], ids, saved_text=saved)
    assert report.held_pages == 1 and report.added == 2
    assert [q["kind"] for q in rec.questions(adm["id"])] == [m.Q_WRONG_PATIENT]


class FakeVoiceModel(FakeExtractor):

    def __init__(self, spoken="Date 21/10\nPR 96/min", fail=False):
        super().__init__()
        self.spoken, self.fail, self.audio_calls = spoken, fail, 0

    async def generate(self, prompt, **kwargs):
        if kwargs.get("audio"):
            self.audio_calls += 1
            if self.fail:
                return "   ", Usage("fake", cost_inr=0.0)
            assert kwargs["audio"][0][1] == "audio/webm"
            return self.spoken, Usage("fake", cost_inr=0.02)
        return await super().generate(prompt, **kwargs)


async def test_voice_note_becomes_a_page_with_a_header_and_facts(setup):
    rec, adm, ids, saved = setup
    voice = rec.add_source(adm["id"], "voice", data=b"made-up audio", mime="audio/webm",
                           received_at="2026-10-21T15:30:00+00:00")[0]
    model = FakeVoiceModel()
    report = await process_batch(rec, model, SETTINGS, adm["id"], [voice["id"]])
    assert model.audio_calls == 1 and report.added == 1 and report.read_cost_inr == 0.02
    page = rec.pages(adm["id"], voice["id"])[0]
    assert page["text"].startswith("Voice note (recorded 21 Oct 2026, 21:00):\n")
    assert "PR 96/min" in page["text"]
    assert rec.source(voice["id"])["status"] == m.SRC_MERGED
    assert [f["value"]["number"] for f in rec.facts(adm["id"])] == [81]


async def test_failed_voice_note_marks_the_source_failed_and_adds_no_facts(setup):
    rec, adm, ids, saved = setup
    voice = rec.add_source(adm["id"], "voice", data=b"made-up audio", mime="audio/webm")[0]
    report = await process_batch(rec, FakeVoiceModel(fail=True), SETTINGS, adm["id"], [voice["id"]])
    assert rec.source(voice["id"])["status"] == m.SRC_FAILED and rec.source(voice["id"])["error"]
    assert report.added == 0 and rec.facts(adm["id"]) == [] and report.errors


def _age(rec, sid, seconds=3600):
    from datetime import datetime, timedelta, timezone
    rec.update_source(sid, received_at=(datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds"))


async def test_stuck_photo_is_picked_up_by_the_next_batch_without_reading_again(setup):
    rec, adm, ids, saved = setup
    first, rest = ids[0], ids[1:]
    await process_batch(rec, FakeExtractor(fail_times=2), SETTINGS, adm["id"], [first], saved_text=saved)
    assert rec.source(first)["status"] == m.SRC_READ
    _age(rec, first)
    extractor = FakeExtractor()
    report = await process_batch(rec, extractor, SETTINGS, adm["id"], rest, saved_text=saved)
    assert {rec.source(s)["status"] for s in ids} == {m.SRC_MERGED}
    assert report.pages_read == len(rest) and report.added == 3


async def test_recent_read_photo_is_left_to_its_own_batch(setup):
    rec, adm, ids, saved = setup
    await process_batch(rec, FakeExtractor(fail_times=2), SETTINGS, adm["id"], [ids[0]], saved_text=saved)
    await process_batch(rec, FakeExtractor(), SETTINGS, adm["id"], ids[1:], saved_text=saved)
    assert rec.source(ids[0])["status"] == m.SRC_READ
