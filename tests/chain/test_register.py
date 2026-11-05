"""The register with made-up readings; no network or paid calls."""
import asyncio
from dataclasses import replace

import pytest

from wardos.chain import ask as asking, model as m, register
from wardos.chain.files import DriveFileStore
from wardos.chain.record import RecordError
from .test_api import _client, FakeModel, SETTINGS

READING = """**Image 1:**
Name: Mira Example
UHID: 20260000001
Age: 37
Sex: Female
Date of admission: 20/10/2026
Date 20/10
PR 88/min
**Image 2:**
Name: Mira Example
UHID: 20260000001
Age/Sex: 37/F
Date of discharge: 22/10/2026
Date 21/10
PR 92/min
**Image 3:**
Name: Wrong Example
UHID: 20260000002
Age: 80
Sex: Male
Date 22/10
PR 90/min
"""


def entry(rid="run_one", **fields):
    return {"id": rid, "run_id": rid, "name": "Mira Example", "uhid": "20260000001", "age": "37", "sex": "F",
            "department": "Surgery", "unit": "Unit 2", "run_date": "2026-10-24", "reading_file_id": "reading",
            "photos": [{"id": f"photo_{i}", "number": i, "mime": "image/jpeg"} for i in range(1, 4)], **fields}


def test_identity_votes_by_page():
    out = register.identity(READING, "2026-10-24")
    assert out == {"name": "Mira Example", "uhid": "20260000001", "age": "37", "sex": "F",
                   "admitted_at": "2026-10-20", "discharged_at": "2026-10-22"}
    assert register.identity("Date 21/10\nPR 88/min\nPhone: 20260000003", "2026-10-24")["uhid"] is None


def test_find_exact_uhid_name_unit_and_order(tmp_path):
    rec = _client(tmp_path).app.state.record
    for doc in (entry(), entry("run_two", run_date="2026-10-25", unit="Unit 1"), entry("other", name="Tara Fiction")):
        rec.store.put(register.COLLECTION, doc)
    assert [d["id"] for d in register.find(rec, "old patient Mira")] == ["run_two", "run_one"]
    assert [d["id"] for d in register.find(rec, "old patient Mira Surgery Unit 2")] == ["run_one"]
    assert len(register.find(rec, "20260000001")) == 3
    assert register.find(rec, "20260000000") == []
    assert register.find(rec, "old patient") == []


def test_sync_skips_known_adds_new_and_repeats(tmp_path, monkeypatch):
    rec = _client(tmp_path).app.state.record
    rec.store.put(register.COLLECTION, entry())
    rows = [{"Run ID": rid, "Status": "Success", "Case folder": "folder"} for rid in ("run_one", "run_two")]
    monkeypatch.setattr(register, "_services", lambda token: (object(), object()))
    monkeypatch.setattr(register, "_runs", lambda sheets: rows)
    indexed = []
    def index(row, token):
        indexed.append(row["Run ID"])
        return entry(row["Run ID"]), None
    monkeypatch.setattr(register, "_index", index)
    out = register.sync(rec)
    assert out["indexed"] == 1 and out["known"] == 1 and indexed == ["run_two"]
    assert register.sync(rec)["indexed"] == 0


def test_index_reads_only_text_orders_photos_and_rejects_mismatch(monkeypatch):
    monkeypatch.setattr(register, "_drive", lambda token: object())
    files = [{"id": f"photo_{i}", "name": f"Page_{i}_abc.jpg", "mimeType": "image/jpeg"} for i in (3, 1, 2)]
    files.append({"id": "reading", "name": "stage1_OCR.txt", "mimeType": "text/plain"})
    monkeypatch.setattr(register, "_walk", lambda drive, folder: files)
    downloads = []
    def download(drive, fid):
        downloads.append(fid)
        return READING.encode()
    monkeypatch.setattr(register, "_download", download)
    row = {"Run ID": "run_one", "Case folder": "https://drive.google.com/drive/folders/folder", "Run date": "2026-10-24"}
    doc, why = register._index(row, "token")
    assert why is None and doc["name"] == "Mira Example"
    assert [p["number"] for p in doc["photos"]] == [1, 2, 3] and downloads == ["reading"]
    files.pop(0)
    assert register._index(row, "token")[1] == "page_mismatch"


def prepare_import(tmp_path, monkeypatch):
    api = _client(tmp_path)
    rec = api.app.state.record
    rec.store.put(register.COLLECTION, entry())
    monkeypatch.setattr(register, "_drive", lambda token: object())
    monkeypatch.setattr(register, "_download", lambda drive, fid: READING.encode())
    return api, rec


def test_pull_twice_one_archived_stay_saved_text_only(tmp_path, monkeypatch):
    api, rec = prepare_import(tmp_path, monkeypatch)
    def no_read(*args):
        raise AssertionError("must not read or copy photos")
    monkeypatch.setattr(rec.files, "save", no_read)
    monkeypatch.setattr(rec.files, "read", no_read)
    settings = replace(SETTINGS, drive_token="fake")
    a = asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=settings))
    b = asyncio.run(register.pull_in(rec, "run_one", "dr_b", model=FakeModel(), settings=settings))
    assert a["id"] == b["id"] and a["old_case"] and a["archived"]
    assert len(rec.store.query(m.ADMISSIONS)) == 1 and len(rec.store.query(m.PATIENTS)) == 1
    assert len(rec.facts(a["id"])) == 3
    assert api.get("/admissions").json() == [] and asking.build_ward(rec) == []
    assert rec.store.get(register.COLLECTION, "run_one")["cost_inr"] == 0
    sources = rec.store.query(m.SOURCES, admission_id=a["id"])
    assert {s["file_uri"] for s in sources} == {"drive:photo_1", "drive:photo_2", "drive:photo_3"}
    assert asking.ask(rec, "vitals", a["id"])["card"]["kind"] == "vitals"
    assert asking.ask(rec, "why was she admitted", a["id"])["card"]["kind"] == "ai"
    assert asking.ask(rec, "old patient Mira", a["id"])["intent"]["action"] == "old_patient"


def test_old_patient_api_and_new_lookup_sync(tmp_path, monkeypatch):
    api = _client(tmp_path)
    rec = api.app.state.record
    called = []
    def sync(record, token):
        called.append(True)
        record.store.put(register.COLLECTION, entry())
    monkeypatch.setattr(register, "sync", sync)
    out = api.post("/ask", json={"text": "old patient Mira", "direct": True})
    assert out.status_code == 200 and out.json()["card"]["kind"] == "old_case"
    assert len(out.json()["card"]["matches"]) == 1
    assert api.post("/ask", json={"text": "20260000001", "direct": True}).json()["card"]["matches"]
    assert len(called) == 2
    assert api.post("/register/find", json={"text": "No Match"}).json()["matches"] == []


def test_processing_claim_refuses_second_import(tmp_path, monkeypatch):
    _, rec = prepare_import(tmp_path, monkeypatch)
    doc = entry(import_status="processing")
    rec.store.put(register.COLLECTION, doc)
    with pytest.raises(RecordError, match="being brought in"):
        asyncio.run(register.pull_in(rec, doc["id"], "dr_a", model=FakeModel(), settings=SETTINGS))
    assert rec.store.query(m.ADMISSIONS) == []


def test_original_photo_uses_drive_id(tmp_path, monkeypatch):
    api = _client(tmp_path)
    rec = api.app.state.record
    p = rec.create_patient(name="Tara Fiction")
    a = rec.create_admission(p["id"], unit="Surgery Unit 2")
    source, duplicate = rec.add_reference_source(a["id"], "drive:original_photo")
    assert not duplicate and rec.add_reference_source(a["id"], "drive:original_photo")[1]
    class Media:
        def execute(self):
            return b"original photo bytes"
    class Drive:
        def files(self): return self
        def get_media(self, *, fileId):
            assert fileId == "original_photo"
            return Media()
    store = DriveFileStore("unused", None)
    monkeypatch.setattr(DriveFileStore, "drive", property(lambda self: Drive()))
    rec.files = store
    out = api.get(f"/sources/{source['id']}/file")
    assert out.status_code == 200 and out.content == b"original photo bytes"


def test_unavailable_completed_runs_are_not_walked_each_lookup(tmp_path, monkeypatch):
    rec = _client(tmp_path).app.state.record
    rows = [{"Run ID": "no_photos", "Status": "Success", "Case folder": "folder"}]
    monkeypatch.setattr(register, "_services", lambda token: (object(), object()))
    monkeypatch.setattr(register, "_runs", lambda sheets: rows)
    calls = []
    def index(row, token):
        calls.append(True)
        return None, "no_reading_or_photos"
    monkeypatch.setattr(register, "_index", index)
    assert register.sync(rec)["no_reading_or_photos"] == 1
    assert register.sync(rec)["indexed"] == 0 and len(calls) == 1
    assert register.find(rec, "old patient Mira") == []


def test_partial_import_retries_same_admission(tmp_path, monkeypatch):
    from wardos.chain import worker
    _, rec = prepare_import(tmp_path, monkeypatch)
    original = worker.process_batch
    async def incomplete(*args, **kwargs):
        return worker.BatchReport(errors=["made up failure"])
    monkeypatch.setattr(worker, "process_batch", incomplete)
    with pytest.raises(RecordError):
        asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=SETTINGS))
    aid = rec.store.get(register.COLLECTION, "run_one")["admission_id"]
    assert rec.admission(aid)["archived"]
    monkeypatch.setattr(worker, "process_batch", original)
    result = asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=SETTINGS))
    assert result["id"] == aid and len(rec.store.query(m.ADMISSIONS)) == 1


def test_new_rows_are_checked_without_reloading_index_after_patient_writes(tmp_path, monkeypatch):
    rec = _client(tmp_path).app.state.record
    rec.store.put(register.COLLECTION, entry())
    rows = [{"Run ID": "run_one", "Status": "Success", "Case folder": "folder"}]
    monkeypatch.setattr(register, "_services", lambda token: (object(), object()))
    calls, queries = [], []
    monkeypatch.setattr(register, "_runs", lambda sheets: calls.append(True) or rows)
    original = rec.store.query
    def query(collection, **equals):
        if collection == register.COLLECTION:
            queries.append(True)
        return original(collection, **equals)
    monkeypatch.setattr(rec.store, "query", query)
    monkeypatch.setattr(register, "_index", lambda row, token: (entry(row["Run ID"]), None))
    register.sync(rec)
    rec.create_patient(name="Unrelated Fiction")
    rows.append({"Run ID": "run_two", "Status": "Success", "Case folder": "folder"})
    assert register.sync(rec)["indexed"] == 1
    assert len(register.find(rec, "Mira")) == 2
    assert len(calls) == 2 and len(queries) == 1


def test_past_name_does_not_select_a_ward_patient_by_shared_surname(tmp_path, monkeypatch):
    api = _client(tmp_path)
    rec = api.app.state.record
    p = rec.create_patient(name="Tara Example")
    rec.create_admission(p["id"], unit="Surgery Unit 2")
    def no_clinical_pages(*args):
        raise AssertionError("an old lookup must not rebuild the ward's clinical pages")
    monkeypatch.setattr(asking, "build_ward", no_clinical_pages)
    assert asking.ask(rec, "old patient Mira Example")["intent"]["action"] == "old_patient"
    assert asking.ask(rec, "20260000001")["intent"]["action"] == "old_patient"


def test_previous_reports_stay_with_selected_old_admission(tmp_path, monkeypatch):
    _, rec = prepare_import(tmp_path, monkeypatch)
    a = asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=SETTINGS))
    result = asking.ask(rec, "previous reports", a["id"])
    assert result["intent"]["action"] == "reports" and result["intent"]["patient_id"] == a["id"]


def test_interrupted_import_expires_and_reuses_admission_and_pages(tmp_path, monkeypatch):
    _, rec = prepare_import(tmp_path, monkeypatch)
    class Interrupted:
        async def generate(self, *args, **kwargs):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=Interrupted(), settings=SETTINGS))
    doc = rec.store.get(register.COLLECTION, "run_one")
    assert doc["import_status"] == "processing"
    aid = doc["admission_id"]
    doc["import_lease_until"] = 0
    rec.store.put(register.COLLECTION, doc)
    a = asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=SETTINGS))
    assert a["id"] == aid and a["archived"]
    assert len(rec.store.query(m.ADMISSIONS)) == 1 and len(rec.store.query(m.SOURCES)) == 3
    assert len(rec.pages(aid)) == 3 and len(rec.facts(aid)) == 3
    assert rec.store.get(register.COLLECTION, "run_one")["import_status"] == "ready"


def test_stale_worker_cannot_call_model_or_overwrite_new_claim(tmp_path):
    rec = _client(tmp_path).app.state.record
    rec.store.put(register.COLLECTION, entry(import_status="processing", import_claim="new", import_lease_until=0))
    class NoModel:
        async def generate(self, *args, **kwargs):
            raise AssertionError("stale worker must not spend model quota")
    with pytest.raises(RecordError, match="resumed elsewhere"):
        asyncio.run(register._ImportModel(NoModel(), rec, "run_one", "old").generate("unused"))
    with pytest.raises(RecordError, match="resumed elsewhere"):
        register._save_import(rec, "run_one", "old", import_status="ready")
    assert rec.store.get(register.COLLECTION, "run_one")["import_claim"] == "new"
    assert register._renew_import(rec, "run_one", "new")
    assert rec.store.get(register.COLLECTION, "run_one")["import_lease_until"] > register.time.time()


def test_ownership_checked_again_after_model_reply(tmp_path):
    rec = _client(tmp_path).app.state.record
    rec.store.put(register.COLLECTION, entry(import_status="processing", import_claim="old", import_lease_until=0))
    class TakenOver:
        async def generate(self, *args, **kwargs):
            rec.store.put(register.COLLECTION, entry(import_status="processing", import_claim="new"))
            return {}, None
    with pytest.raises(RecordError, match="resumed elsewhere"):
        asyncio.run(register._ImportModel(TakenOver(), rec, "run_one", "old").generate("unused"))
    assert rec.store.get(register.COLLECTION, "run_one")["import_claim"] == "new"


def test_name_alone_not_on_ward_offers_old_stays_and_ordinary_questions_do_not(tmp_path, monkeypatch):
    api = _client(tmp_path)
    rec = api.app.state.record
    monkeypatch.setattr(register, "sync", lambda record, token: record.store.put(register.COLLECTION, entry()))
    rec.store.put(register.COLLECTION, entry())
    out = api.post("/ask", json={"text": "miraa", "direct": True}).json()
    assert out["card"]["kind"] == "old_case" and len(out["card"]["matches"]) == 1
    assert out["card"]["text"].startswith("Not on your ward")
    for text in ("what was the sodium", "any bleeding", "why admitted"):
        assert api.post("/ask", json={"text": text, "direct": True}).json()["card"]["kind"] != "old_case"


def test_name_read_after_bringing_in_is_searchable(tmp_path, monkeypatch):
    api, rec = prepare_import(tmp_path, monkeypatch)
    rec.store.put(register.COLLECTION, entry(name="Zinnu Fiction"))
    settings = replace(SETTINGS, drive_token="fake")
    stay = asyncio.run(register.pull_in(rec, "run_one", "dr_a", model=FakeModel(), settings=settings))
    assert register.find(rec, "old patient Zorawari") == []
    patient = rec.store.get(m.PATIENTS, stay["patient_id"])
    rec.store.put(m.PATIENTS, dict(patient, name="Zorawari Fiction"))
    assert [d["run_id"] for d in register.find(rec, "old patient zorwaari")] == ["run_one"]
