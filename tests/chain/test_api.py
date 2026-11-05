"""Offline API test with a fake model: upload, duplicates, conflicts and the doctor's answers."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from wardos.chain.api import create_app
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import ChainSettings
from wardos.models import Usage

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6, store="sqlite", files="local")


class FakeModel:

    async def generate(self, prompt, **kwargs):
        pages = re.split(r"^=== PAGE (\d+) ===$", prompt, flags=re.M)[1:]
        facts, info = [], []
        for number, text in zip(pages[::2], pages[1::2]):
            day = re.search(r"Date (\d+)/10", text)
            when = f"2026-10-{int(day.group(1)):02d}" if day else None
            info.append({"p": int(number), "type": "rounds_note", "date": when})
            for pr in re.findall(r"PR (\d+)/min", text):
                facts.append({"p": int(number), "t": "vital", "k": "pulse", "v": f"{pr}/min", "n": int(pr), "w": when,
                              "q": f"PR {pr}/min"})
        return {"pages": info, "facts": facts}, Usage("fake", cost_inr=0.0)


class InlineQueue:

    def __init__(self):
        self.app = None

    async def enqueue(self, admission_id, source_ids, **_):
        await self.app.state.run_batch(admission_id, source_ids)


def _client(tmp_path):
    queue = InlineQueue()
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")),
                     model=FakeModel(), settings=SETTINGS, queue=queue)
    queue.app = app
    return TestClient(app)


def test_stay_through_the_api(tmp_path):
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "Test Patient"}).json()
    adm = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2",
                                        "admitted_at": "2026-10-20"}).json()
    a = adm["id"]
    assert api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 88/min"}).json()["duplicate"] is False
    assert api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 88/min"}).json()["duplicate"] is True
    api.post(f"/admissions/{a}/reviewed")
    api.post(f"/admissions/{a}/notes", json={"text": "Date 20/10\nPR 120/min"})
    questions = api.get(f"/admissions/{a}/questions").json()
    assert len(questions) == 1 and questions[0]["kind"] == "conflict"
    changed = api.get(f"/admissions/{a}/since-review").json()
    assert changed, "the conflicting note is new since dr_a's review"
    pulses = [f for f in api.get(f"/admissions/{a}/facts", params={"type": "vital"}).json()]
    assert {f["status"] for f in pulses} == {"disputed"}
    fixed = api.patch(f"/facts/{pulses[0]['id']}", json={"action": "correct", "text": "90/min", "number": 90})
    assert fixed.status_code == 200
    live = api.get(f"/admissions/{a}/facts", params={"type": "vital"}).json()
    assert [f["value"]["text"] for f in live] == ["90/min"] and live[0]["status"] == "doctor_locked"
    assert api.get(f"/admissions/{a}/questions").json() == []
    overview = api.get(f"/admissions/{a}").json()
    assert overview["sources"] == {"merged": 2} and overview["facts"] == 1 and overview["open_questions"] == 0


def test_refusals(tmp_path):
    api = _client(tmp_path)
    assert api.get("/admissions/nope").status_code == 404
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    out = api.post(f"/admissions/{a}/uploads", files=[("files", ("x.docx", b"PK..", "application/msword"))]).json()
    assert out["queued"] == 0 and "not a photo or PDF" in out["files"][0]["error"]
    assert api.patch("/facts/x", json={"action": "delete"}).status_code == 400
    assert api.post(f"/admissions/{a}/notes", json={"text": "  "}).status_code == 400


def test_import_uses_the_existing_reading_and_never_reads_the_photo(tmp_path):
    import json
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2",
                                      "admitted_at": "2026-10-20"}).json()["id"]
    files = [("files", ("p1.jpg", b"\xff\xd8 photo one", "image/jpeg")), ("files", ("p2.jpg", b"\xff\xd8 two", "image/jpeg"))]
    out = api.post(f"/admissions/{a}/import", files=files,
                   data={"readings": json.dumps(["Date 20/10\nPR 88/min", "Date 21/10\nPR 92/min"])}).json()
    assert out["queued"] == 2
    facts = api.get(f"/admissions/{a}/facts").json()
    assert sorted(f["value"]["text"] for f in facts) == ["88/min", "92/min"]
    again = api.post(f"/admissions/{a}/import", files=files[:1], data={"readings": json.dumps(["x"])}).json()
    assert again["queued"] == 0 and again["files"][0]["duplicate"] is True
    bad = api.post(f"/admissions/{a}/import", files=files[:1], data={"readings": json.dumps([])})
    assert bad.status_code == 400


class FakeDrive:

    def __init__(self):
        self.items, self.counter, self.result = {}, 0, None

    def files(self):
        return self

    def create(self, body, media_body=None, fields=None):
        self.counter += 1
        fid = f"id{self.counter}"
        self.items[fid] = body
        return _Call({"id": fid})

    def list(self, q, fields=None, pageSize=None):
        import re as _re
        if "result.json" in q:
            return _Call({"files": [{"id": "resultfile"}] if self.result is not None else []})
        name = _re.search(r"name = '([^']+)'", q).group(1)
        return _Call({"files": [{"id": i} for i, b in self.items.items() if b["name"] == name][:1]})

    def get_media(self, fileId):
        import json as _json
        return _Call(_json.dumps(self.result).encode())


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _final_client(tmp_path, monkeypatch):
    from wardos.chain import finals
    drive, runs = FakeDrive(), []
    monkeypatch.setattr(finals, "_drive", lambda creds: drive)
    queue = InlineQueue()
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")),
                     model=FakeModel(), settings=SETTINGS, queue=queue,
                     job_runner=lambda env: runs.append(env) or "executions/e1", drive_creds=object())
    queue.app = app
    api = TestClient(app)
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    api.post(f"/admissions/{a}/import", files=[("files", ("p.jpg", b"\xff\xd8 img", "image/jpeg"))],
             data={"readings": '["Date 20/10\\nPR 88/min"]'})
    return api, a, drive, runs


def test_final_summary_start_status_and_refusal(tmp_path, monkeypatch):
    api, a, drive, runs = _final_client(tmp_path, monkeypatch)
    assert api.get(f"/admissions/{a}/final").json() is None
    started = api.post(f"/admissions/{a}/final").json()
    assert started["status"] == "running" and started["execution"] == "executions/e1"
    assert runs[0]["WARDOS_FINAL_RUN_ID"] == started["id"] and runs[0]["WARDOS_FINAL_INPUT_FOLDER_ID"] == started["folder_id"]
    names = sorted(b["name"] for b in drive.items.values())
    assert "reading.txt" in names and "input.json" in names and "01.jpg" in names
    assert api.post(f"/admissions/{a}/final").status_code == 409
    assert api.get(f"/admissions/{a}/final").json()["status"] == "running"
    drive.result = {"run_id": started["id"], "status": "done", "doc_url": "https://docs.example/d/1", "cost": 11.5,
                    "warnings": [], "error": None}
    done = api.get(f"/admissions/{a}/final").json()
    assert done["status"] == "done" and done["doc_url"] == "https://docs.example/d/1" and done["cost"] == 11.5
    assert api.get(f"/admissions/{a}/final").json()["status"] == "done"
    assert api.post(f"/admissions/{a}/final").status_code == 200
    assert api.post("/admissions/nope/final").status_code == 404


def test_final_summary_error_is_kept(tmp_path, monkeypatch):
    api, a, drive, runs = _final_client(tmp_path, monkeypatch)
    api.post(f"/admissions/{a}/final")
    drive.result = {"status": "error", "error": "boom"}
    failed = api.get(f"/admissions/{a}/final").json()
    assert failed["status"] == "error" and failed["error"] == "boom"
    assert api.get("/").status_code == 200


def test_screen_and_ward_list_and_source_file(tmp_path):
    api = _client(tmp_path)
    page = api.get("/")
    assert page.status_code == 200 and "WardOS" in page.text and "#EDF6F2" in page.text
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    ward = api.get("/admissions").json()
    assert [w["admission"]["id"] for w in ward] == [a] and ward[0]["patient"]["name"] == "T"
    import json
    out = api.post(f"/admissions/{a}/import", files=[("files", ("p.jpg", b"\xff\xd8 img", "image/jpeg"))],
                   data={"readings": json.dumps(["Date 20/10\nPR 88/min"])}).json()
    sid = out["files"][0]["source_id"]
    photo = api.get(f"/sources/{sid}/file")
    assert photo.status_code == 200 and photo.content == b"\xff\xd8 img" and photo.headers["content-type"] == "image/jpeg"
    assert api.get(f"/sources/{sid}/pages").json()[0]["text"].startswith("Date 20/10")
    assert api.get("/sources/nope/file").status_code == 404


def test_voice_route_takes_a_recording_and_refuses_big_or_non_audio(tmp_path):
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    url = f"/admissions/{a}/voice"
    big = api.post(url, files={"file": ("v.webm", b"x" * (12 * 1024 * 1024), "audio/webm")})
    assert big.status_code == 413
    assert api.post(url, files={"file": ("v.txt", b"hello", "text/plain")}).status_code == 400
    assert api.post(url, files={"file": ("v.webm", b"", "audio/webm")}).status_code == 400
    assert api.post("/admissions/nope/voice", files={"file": ("v.webm", b"abc", "audio/webm")}).status_code == 404
    ok = api.post(url, files={"file": ("v.webm", b"made-up audio", "audio/webm;codecs=opus")})
    assert ok.status_code == 200 and ok.json()["duplicate"] is False
    sid = ok.json()["source_id"]
    assert api.post(url, files={"file": ("v.webm", b"made-up audio", "audio/webm")}).json() == {
        "source_id": sid, "duplicate": True}
    assert api.get(f"/sources/{sid}/file").headers["content-type"] == "audio/webm"


def test_final_that_never_writes_a_result_gives_up_after_25_minutes(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from wardos.chain import model as m
    api, a, drive, runs = _final_client(tmp_path, monkeypatch)
    started = api.post(f"/admissions/{a}/final").json()
    record = api.app.state.record
    fresh = record.store.get(m.FINALS, started["id"])
    record.store.put(m.FINALS, dict(fresh, started_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()))
    assert api.get(f"/admissions/{a}/final").json()["status"] == "running"
    record.store.put(m.FINALS, dict(fresh, started_at=(datetime.now(timezone.utc) - timedelta(minutes=26)).isoformat()))
    dead = api.get(f"/admissions/{a}/final").json()
    assert dead["status"] == "error" and "did not finish" in dead["error"] and "25 min" in dead["error"]
    assert api.post(f"/admissions/{a}/final").status_code == 200


def test_upload_answers_in_order_with_refusals_and_repeats(tmp_path):
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "T"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    files = [("files", (f"p{i}.pdf", f"%PDF made-up {i}".encode(), "application/pdf")) for i in range(6)]
    files.insert(2, ("files", ("x.docx", b"PK..", "application/msword")))
    files.append(("files", ("again.pdf", b"%PDF made-up 0", "application/pdf")))
    out = api.post(f"/admissions/{a}/uploads", files=files).json()
    names = [f["filename"] for f in out["files"]]
    assert names == ["p0.pdf", "p1.pdf", "x.docx", "p2.pdf", "p3.pdf", "p4.pdf", "p5.pdf", "again.pdf"]
    assert "error" in out["files"][2] and out["queued"] == 6
    assert out["files"][7]["duplicate"] is True and out["files"][7]["source_id"] == out["files"][0]["source_id"]
    ids = [f["source_id"] for f in out["files"] if "source_id" in f]
    assert len(set(ids)) == 6
    again = api.post(f"/admissions/{a}/uploads", files=files[:2]).json()
    assert [f["duplicate"] for f in again["files"]] == [True, True] and again["queued"] == 0
    assert len(api.app.state.record.store.query("sources", admission_id=a)) == 6


def test_screen_has_two_layouts_the_ask_bar_and_none_of_the_old_cards(tmp_path):
    page = _client(tmp_path).get("/").text
    assert 'id="cols"' in page and "innerWidth >= 820" in page
    assert 'fetch(' in page and '"/ask"' in page and "Check, then Send" in page and "Ask WardOS" in page
    assert "SpeechRecognition" in page and "listenRecorded" in page  # the phone's own speech first, Muse recording as the fallback
    assert "top_cards" not in page and ".tops" not in page and "--rose" not in page and "#FFF4F5" not in page
    assert 'id="hints"' in page and 'id="getReports"' not in page and 'class="acts"' not in page and "Demo · names changed" in page
    assert "agentfail" not in page and "Agent at work" not in page and "fake-api" not in page
    assert "COMPRESS_CEILING_MB = 0.32" in page and "COMPRESS_MIN_LONG_EDGE_PX = 1800" in page
    assert "X-WardOS-Doctor" not in page and '"/auth/me"' in page
    assert "/final" in page and "MediaRecorder" in page


def test_today_page_no_longer_sends_top_cards_and_ask_route_is_listed(tmp_path):
    api = _client(tmp_path)
    patient = api.post("/patients", json={"name": "Test Patient"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2", "bed": "3"}).json()["id"]
    today = api.get(f"/admissions/{a}/today").json()
    assert "top_cards" not in today and {"alerts", "notices", "brief"} <= set(today)
    assert "/ask" in api.get("/openapi.json").json()["paths"]


def test_retry_route_and_stuck_list(tmp_path):
    from datetime import datetime, timedelta, timezone
    api = _client(tmp_path)
    record = api.app.state.record
    patient = api.post("/patients", json={"name": "Test Patient"}).json()
    a = api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2", "admitted_at": "2026-10-20"}).json()["id"]
    src, _ = record.add_source(a, "photo", data=b"stuck photo", mime="image/jpeg")
    record.save_pages(src["id"], [{"number": 1, "text": "Date 20/10\nPR 88/min"}])
    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(timespec="seconds")
    record.update_source(src["id"], status="read", received_at=old)
    today = api.get(f"/admissions/{a}/today")
    assert today.status_code == 200 and today.json()["stuck"] == [{"id": src["id"], "kind": "photo"}]
    assert api.post("/sources/nope/retry").status_code == 404
    assert api.post(f"/sources/{src['id']}/retry").json() == {"queued": 1}
    assert record.source(src["id"])["status"] == "merged"
    assert api.post(f"/sources/{src['id']}/retry").status_code == 409


def test_final_photos_go_up_side_by_side_on_their_own_drive_clients(tmp_path, monkeypatch):
    import threading
    from wardos.chain import finals
    api, a, drive, runs = _final_client(tmp_path, monkeypatch)
    made = []
    monkeypatch.setattr(finals, "_drive", lambda creds: made.append(threading.get_ident()) or drive)
    api.post(f"/admissions/{a}/final")
    assert len(made) >= 2  # one for the folders, one per upload worker
    assert "reading.txt" in {b["name"] for b in drive.items.values()}


def test_demo_final_cap_and_gate(tmp_path, monkeypatch):
    from wardos.chain import api as chain_api, demo
    assert chain_api.DEMO_FINALS_PER_COPY == 3
    refused = demo.GuestAuth(SQLiteDocStore(tmp_path / "g.sqlite3"))
    allowed = demo.GuestAuth(SQLiteDocStore(tmp_path / "g2.sqlite3"), final_ok=True)

    class Req:
        class url:
            path = "/admissions/adm_x/final"
    assert refused.gate(Req)[1].status_code == 403
    assert allowed.gate(Req)[1] is None
