"""Old stays stay read-only, photo-bed history, cache writes and voice fallback."""
from pathlib import Path

import pytest

from wardos.chain import model as m
from wardos.chain.ask import ask
from wardos.chain.docstore import CachedDocStore, SQLiteDocStore
from wardos.models import Usage
from .test_api import _client
from .test_beds import stay


def test_history_uses_the_photo_bed_and_keeps_explicit_clear(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Mira", None)
    r = api.app.state.record
    r.store.put(m.FACTS, {"id": "bed-photo", "admission_id": a["id"], "type": "identity", "key": "bed",
                          "value": {"text": "5"}, "status": m.ACTIVE})
    assert api.get(f"/admissions/{a['id']}/today").json()["admission"]["bed"] == "5"
    r.set_bed(a["id"], doctor="Test doctor", bed="9")
    r.set_bed(a["id"], doctor="Test doctor", bed=None)
    r.set_bed(a["id"], doctor="Test doctor", bed="10")
    events = [e for e in r.events(a["id"]) if e["type"] == "bed.set"]
    assert [(e["detail"]["from"], e["detail"]["bed"]) for e in events] == [("5", "9"), ("9", None), (None, "10")]


def old_stay(api):
    a = stay(api, "Test Mira", "5")
    r = api.app.state.record
    api.post(f"/admissions/{a['id']}/notes", json={"text": "Date 20/10\nPR 88/min"})
    api.post(f"/admissions/{a['id']}/notes", json={"text": "Date 20/10\nPR 120/min"})
    api.post(f"/admissions/{a['id']}/notes", json={"text": "Date 23/10\nPR 140/min"})
    a = r.admission(a["id"])
    r.store.put(m.ADMISSIONS, dict(a, old_case=True, archived=True, old_run_date="2026-10-25"))
    return a["id"]


def test_old_stay_uses_own_entry_dates_and_read_only_asks(tmp_path):
    api = _client(tmp_path)
    aid = old_stay(api)
    page = api.get(f"/admissions/{aid}/today").json()
    assert page["admission"]["stay_label"] == "Old stay · Summary service · 2026-10-20 to 2026-10-23"
    assert page["admission"]["admitted_text"] is None and page["admission"]["day"] is None
    assert page["brief"]["lines"][2].startswith("Out of range in that stay:")
    assert page["brief"]["lines"][3] == ""
    assert not page["notices"] and page["flag"]["flag"] is None
    r = api.app.state.record
    assert ask(r, "brief", aid)["card"]["read_only"]
    for words in ("set bed 9", "note plan tomorrow", "discharge summary"):
        card = ask(r, words, aid)["card"]
        assert card["kind"] == "message" and "view-and-ask" in card["text"]
    assert ask(r, "vitals", aid)["card"]["kind"] == "vitals"
    assert not api.get("/admissions").json()


def test_old_stay_rejects_all_screen_writes_before_any_work(tmp_path):
    api = _client(tmp_path)
    aid = old_stay(api)
    base = f"/admissions/{aid}"
    r = api.app.state.record
    version = r.admission(aid)["version"]
    requests = [("patch", base, {"bed": "9"}), ("patch", base + "/severity", {"severity": "watch"}),
                ("post", base + "/notes", {"text": "note"}), ("post", base + "/reviewed", {}),
                ("post", base + "/draft", {}), ("post", base + "/final", {})]
    for method, url, body in requests:
        assert getattr(api, method)(url, json=body).status_code == 409, url
    for route, field in (("uploads", "files"), ("voice", "file"), ("reports", "file")):
        assert api.post(base + "/" + route, files={field: ("test.wav", b"test", "audio/wav")}).status_code == 409
    q = r.questions(aid)[0]
    assert api.post(f"/questions/{q['id']}/answer", json={"answer": "test"}).status_code == 409
    f = r.facts(aid)[0]
    assert api.patch(f"/facts/{f['id']}", json={"action": "confirm"}).status_code == 409
    assert r.admission(aid)["version"] == version


def test_bed_and_mark_writes_keep_cached_clinical_pages(tmp_path):
    from .test_docstore_cache import Counting
    raw = Counting(SQLiteDocStore(tmp_path / "r.sqlite3"))
    store = CachedDocStore(raw, ttl=60)
    for c in (m.FACTS, m.PAGES, m.ADMISSIONS):
        store.put(c, {"id": "one", "admission_id": "a", "bed": "5"})
        store.query(c, admission_id="a")
    before = raw.reads
    store.transact(lambda tx: tx.put(m.ADMISSIONS, dict(tx.get(m.ADMISSIONS, "one"), bed="9")))
    store.query(m.FACTS, admission_id="a")
    store.query(m.PAGES, admission_id="a")
    assert raw.reads == before
    assert store.query(m.ADMISSIONS, admission_id="a")[0]["bed"] == "9"
    assert raw.reads == before + 1


def test_voice_fallback_returns_words_without_writing_or_queueing(tmp_path):
    api = _client(tmp_path)
    class Speech:
        async def generate(self, prompt, **kwargs):
            assert kwargs["audio"] == [(b"real-audio-in-other-test", "audio/wav")]
            return "naya patient bed 19 Test Mira", Usage("fake", cost_inr=0)
    from wardos.chain.api import create_app
    from fastapi.testclient import TestClient
    api = TestClient(create_app(record=api.app.state.record, model=Speech()))
    result = api.post("/voice/transcribe", files={"file": ("ask.wav", b"real-audio-in-other-test", "audio/wav")})
    assert result.status_code == 200 and result.json()["text"].startswith("naya patient")
    assert not api.app.state.record.store.query(m.ADMISSIONS)
    assert not api.app.state.record.store.query(m.SOURCES)
    assert api.post("/voice/transcribe", files={"file": ("x", b"", "audio/wav")}).status_code == 400
    assert api.post("/voice/transcribe", files={"file": ("x", b"x", "text/plain")}).status_code == 400


def test_old_stay_never_sends_push(tmp_path, monkeypatch):
    api = _client(tmp_path)
    aid = old_stay(api)
    alerts = api.app.state.alerts
    from wardos.chain.settings import PushSettings
    alerts.settings = PushSettings(public="test", private="test", subject="mailto:test@example.invalid")
    def forbidden(*args, **kwargs):
        raise AssertionError("old stay must not read subscriptions or send")
    monkeypatch.setattr(alerts, "_devices", forbidden)
    monkeypatch.setattr(alerts, "_send", forbidden)
    assert alerts.after_merge(aid, {}, report_ids=["report"]) == {"sent": 0, "failed": 0}


def test_hindi_voice_command_keeps_name_and_reads_fields(tmp_path):
    r = _client(tmp_path).app.state.record
    result = ask(r, "नया मरीज बेड ४१ नाम आशा उम्र ५८ साल महिला मेडिसिन यूनिट १")
    assert result["card"]["kind"] == "new_patient"
    fields = result["card"]["fields"]
    assert (fields["name"], fields["bed"], fields["age"], fields["sex"], fields["unit"]) == (
        "आशा", "41", "58", "F", "Medicine Unit 1")


def test_new_stay_is_one_atomic_write_and_returns_ready_page(tmp_path):
    api = _client(tmp_path)
    response = api.post("/patients/admit", json={"name": "Test Mira", "unit": "Medicine Unit 1", "bed": "bed 19"})
    assert response.status_code == 200
    out = response.json()
    r = api.app.state.record
    assert r.patient_of(out["admission"]["id"])["id"] == out["patient"]["id"]
    assert out["today"]["admission"]["bed"] == "19"
    assert api.post("/patients/admit", json={"unit": "Medicine Unit 1", "bed": "<bad>"}).status_code == 400
    assert len(r.store.query(m.PATIENTS)) == len(r.store.query(m.ADMISSIONS)) == 1
