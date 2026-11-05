"""Bed changes need confirmation, keep history and stay inside the ward."""
import pytest

from wardos.chain import model as m
from wardos.chain.ask import ask
from wardos.chain.today import build_today
from .test_api import _client


def stay(api, name, bed, ward="A", unit="Medicine Unit 1"):
    patient = api.post("/patients", json={"name": name}).json()
    return api.post("/admissions", json={"patient_id": patient["id"], "unit": unit,
                                        "bed": bed, "ward": ward}).json()


def test_route_records_doctor_time_old_bed_and_retains_history(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Aashvi", "5")
    path = f"/admissions/{a['id']}"
    first = api.patch(path, json={"bed": "bed 9"})
    assert first.status_code == 200
    assert first.json()["bed"] == "9" and first.json()["bed_set_by"] and first.json()["bed_set_at"]
    assert api.patch(path, json={"bed": "10"}).status_code == 200
    events = [e for e in api.get(path + "/events").json() if e["type"] == "bed.set"]
    assert [(e["detail"]["from"], e["detail"]["bed"]) for e in events] == [("5", "9"), ("9", "10")]
    assert all(e["detail"]["doctor"] and e["created_at"] for e in events)
    assert api.patch(path, json={"bed": "10"}).json()["version"] == 2
    assert api.patch(path, json={"bed": "<bad>"}).status_code == 400
    assert api.patch("/admissions/missing", json={"bed": "1"}).status_code == 404


def test_taken_bed_requires_explicit_confirmation_and_does_not_move_other_patient(tmp_path):
    api = _client(tmp_path)
    a, b = stay(api, "Test Aashvi", "5"), stay(api, "Test Sumedha", "7")
    path = f"/admissions/{a['id']}"
    response = api.patch(path, json={"bed": "7"})
    assert response.status_code == 409 and response.json()["detail"]["occupied"] == [b["id"]]
    assert api.app.state.record.admission(a["id"])["bed"] == "5"
    assert api.patch(path, json={"bed": "7", "confirm_occupied": True}).status_code == 200
    assert api.app.state.record.admission(b["id"])["bed"] == "7"
    stay(api, "Other ward", "9", ward="B")
    stay(api, "Other unit", "9", unit="Surgery Unit 1")
    assert api.patch(path, json={"bed": "9"}).status_code == 200


@pytest.mark.parametrize("words,source,destination", [
    ("Aashvi bed 7", "a", "7"), ("move bed 5 to bed 9", "a", "9"),
    ("bed 7 pe shift", "a", "7"), ("बेड 7 पर शिफ्ट", "a", "7"),
    ("set bed 12", "a", "12"), ("change Sumedha to bed 13", "b", "13"),
])
def test_asks_return_confirm_card_without_writing(tmp_path, words, source, destination):
    api = _client(tmp_path)
    ids = {"a": stay(api, "Test Aashvi", "5")["id"], "b": stay(api, "Test Sumedha", "7")["id"]}
    card = ask(api.app.state.record, words, ids["a"])["card"]
    assert card["kind"] == "set_bed" and card["patient"]["id"] == ids[source] and card["bed"] == destination
    if destination == "7":
        assert card["occupied"]
    assert api.app.state.record.admission(ids["a"])["bed"] == "5"


def test_plain_bed_and_fixed_asks_are_not_moves_and_missing_source_does_not_use_current(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Aashvi", "5")
    for text in ("bed 5", "bed 5 labs", "reports bed 5", "new patient bed 9 Test Ranjika"):
        assert ask(api.app.state.record, text, a["id"])["intent"]["action"] != "set_bed"
    card = ask(api.app.state.record, "move bed 99 to bed 9", a["id"])["card"]
    assert card["kind"] == "message" and "can't find" in card["text"]


def test_clear_bed_does_not_resurrect_old_photo_bed(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Test Aashvi", "5")
    rec = api.app.state.record
    a = rec.set_bed(a["id"], doctor="Test doctor", bed=None)
    old = {"id": "old", "type": "identity", "key": "bed", "value": {"text": "5"}, "status": m.ACTIVE}
    assert build_today(a, rec.patient_of(a["id"]), [old], [])["admission"]["bed"] is None


def test_bed_command_with_shared_name_asks_which_patient(tmp_path):
    api = _client(tmp_path)
    a = stay(api, "Aashvi Devi", "5")
    stay(api, "Aashvi Singh", "8")
    card = ask(api.app.state.record, "Aashvi bed 7", a["id"])["card"]
    assert card["kind"] == "choose" and card["retry_text"] == "set bed 7"
    unknown = ask(api.app.state.record, "move Missingname to bed 9", a["id"])["card"]
    assert unknown["kind"] == "message" and unknown["patient"] is None


def test_voice_storage_failure_can_be_retried_without_exposing_internal_error(tmp_path, monkeypatch):
    import ssl
    api = _client(tmp_path)
    a = stay(api, "Test Aashvi", "5")
    rec = api.app.state.record
    save = rec.files.save
    def fail(*args, **kwargs):
        raise ssl.SSLEOFError("private storage transport detail")
    monkeypatch.setattr(rec.files, "save", fail)
    path = f"/admissions/{a['id']}/voice"
    response = api.post(path, files={"file": ("note.wav", b"RIFF-real-format-not-needed", "audio/wav")})
    assert response.status_code == 503 and "private storage" not in response.text
    assert not rec.store.query(m.SOURCES, admission_id=a["id"])
    monkeypatch.setattr(rec.files, "save", save)
    assert api.post(path, files={"file": ("note.wav", b"RIFF-real-format-not-needed", "audio/wav")}).status_code == 200
    assert len(rec.store.query(m.SOURCES, admission_id=a["id"])) == 1
