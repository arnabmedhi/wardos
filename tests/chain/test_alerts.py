"""Alerts with made-up values, fake push services, no model or cloud calls."""
from concurrent.futures import ThreadPoolExecutor
import base64
import io
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from wardos.chain.alerts import Alerts, SENT, SUBS, SubscribeIn, new_notifications, validate_subscription
from wardos.chain.api import create_app
from wardos.chain.auth import Auth, Doctor, SESSION_COOKIE
from wardos.chain.docstore import CachedDocStore, SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import AuthSettings, PushSettings

from .test_api import FakeModel, InlineQueue, SETTINGS

OWNER, DOCTOR, REMOVED = "owner@example.test", "doctor@example.test", "removed@example.test"
AUTH = AuthSettings(cookie_secret="synthetic-cookie-secret", cookie_secure=False, owner_emails=(OWNER,))
PUSH = PushSettings(public="synthetic-public-key", private="synthetic-private-key", subject="mailto:owner@example.test")


def subscription(label="one"):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/synthetic-{label}",
            "keys": {"p256dh": encode(key), "auth": encode(b"0123456789abcdef")}}


def danger(value="120", time="2026-10-25T08:00"):
    return {"kind": "danger", "key": "pulse", "value": value, "unit": "/min", "time": time,
            "fact_id": "synthetic-fact", "source_id": "synthetic-source"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDOS_DEV_DOCTOR", raising=False)
    record = Record(CachedDocStore(SQLiteDocStore(tmp_path / "r.sqlite3")), LocalFileStore(tmp_path / "files"))
    record.store.put("doctors", {"id": DOCTOR, "email": DOCTOR, "name": "Test Doctor", "active": True})
    auth = Auth(AUTH, record.store)
    sent = []
    service = Alerts(record, auth, settings=PUSH, sender=lambda sub, payload: sent.append((sub, payload)))
    patient = record.create_patient(name="Synthetic Person")
    admission = record.create_admission(patient["id"], unit="Synthetic Unit", bed="12", admitted_at="2026-10-20")
    doctor = Doctor(DOCTOR, "Test Doctor", "doctor")
    service.subscribe(doctor, SubscribeIn(subscription=subscription()))
    return SimpleNamespace(record=record, service=service, doctor=doctor, admission=admission, sent=sent)


def test_pure_delta_ignores_changed_ids_and_remembers_sent_values():
    old = danger()
    changed_id = {**old, "fact_id": "rephoto", "source_id": "other"}
    assert new_notifications({"alerts": [old]}, {"alerts": [changed_id]}) == []
    event = new_notifications({}, {"alerts": [old]})[0]
    assert new_notifications({}, {"alerts": [changed_id]}, [event["key"]]) == []
    assert len(new_notifications({"alerts": [old]}, {"alerts": [danger("130")]})) == 1
    assert len(new_notifications({"alerts": [old]}, {"alerts": [danger(time="2026-10-26T08:00")]})) == 1


def test_new_danger_sends_once_even_with_two_services(world, monkeypatch):
    service = world.service
    monkeypatch.setattr(service, "snapshot", lambda _: {"alerts": [danger()]})
    second = Alerts(world.record, service.auth, settings=PUSH, sender=service.sender)
    monkeypatch.setattr(second, "snapshot", service.snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: s.after_merge(world.admission["id"], {}), [service, second]))
    assert sum(r["sent"] for r in results) == 1
    assert service.after_merge(world.admission["id"], {})["sent"] == 0
    assert len(world.record.store.query(SENT)) == 1
    assert world.sent[0][1]["title"] == "WardOS: Bed 12 - dangerous value. Tap to open."
    assert world.sent[0][1]["url"] == "/#/a/" + world.admission["id"]
    assert "Synthetic Person" not in json.dumps(world.sent[0][1])
    assert world.sent[0][1]["silent"] is False


def test_new_report_is_quiet_and_deduplicated(world):
    service, aid = world.service, world.admission["id"]
    assert service.after_merge(aid, service.snapshot(aid), report_ids=["synthetic-report"])["sent"] == 1
    assert service.after_merge(aid, {}, report_ids=["synthetic-report"])["sent"] == 0
    assert world.sent[0][1]["title"] == "WardOS: Bed 12 - new report."
    assert world.sent[0][1]["silent"] is True


def test_new_notice_report_is_detected_by_the_shared_pure_function():
    notice = {"kind": "info", "title": "New report: lab tests", "source_id": "synthetic-report"}
    page = {"notices": [notice]}
    assert new_notifications({}, page)[0]["silent"] is True
    assert new_notifications(page, page) == []
    assert new_notifications({}, {"notices": [{"kind": "need", "title": "Needs your answer"}]}) == []


@pytest.mark.parametrize("code", [404, 410])
def test_gone_subscription_is_cleared(world, code):
    class Gone(Exception):
        response = SimpleNamespace(status_code=code)
    def sender(*args):
        raise Gone("never print an endpoint or credentials")
    world.service.sender = sender
    assert world.service.test(world.doctor) == {"sent": 0, "failed": 1}
    assert world.record.store.query(SUBS, active=True) == []
    assert world.record.store.query(SUBS)[0]["subscription"] == {}


def test_one_failed_device_does_not_block_the_other(world):
    sub = subscription("two")
    world.service.subscribe(world.doctor, SubscribeIn(subscription=sub))
    def sender(subscription, payload):
        if subscription["endpoint"] == sub["endpoint"]:
            raise RuntimeError("push outage")
    world.service.sender = sender
    assert world.service.test(world.doctor) == {"sent": 1, "failed": 1}
    assert len(world.record.store.query(SUBS, active=True)) == 2


def test_off_is_bound_to_the_doctor_and_removed_doctors_get_nothing(world):
    endpoint = world.record.store.query(SUBS)[0]["subscription"]["endpoint"]
    world.service.unsubscribe(Doctor(OWNER, "Owner", "owner"), endpoint)
    assert len(world.record.store.query(SUBS, active=True)) == 1
    world.service.unsubscribe(world.doctor, endpoint)
    assert world.record.store.query(SUBS, active=True) == []
    world.service.subscribe(world.doctor, SubscribeIn(subscription=subscription()))
    world.record.store.put("doctors", {"id": DOCTOR, "email": DOCTOR, "active": False})
    world.service.auth.forget_list()
    assert world.service.test(world.doctor) == {"sent": 0, "failed": 0}


def test_local_dev_doctor_can_receive_the_owner_test(world):
    from wardos.chain.auth import DEV_EMAIL
    auth = Auth(AuthSettings(dev_doctor="Local test"), world.record.store)
    service = Alerts(world.record, auth, settings=PUSH, sender=world.service.sender)
    doctor = Doctor(DEV_EMAIL, "Local test", "owner")
    service.subscribe(doctor, SubscribeIn(subscription=subscription("dev")))
    assert service.test(doctor) == {"sent": 1, "failed": 0}


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1/private", "http://fcm.googleapis.com/send",
                                      "https://fcm.googleapis.com.evil.test/send", "https://web.push.apple.com:444/x",
                                      "https://user@fcm.googleapis.com/send", [], None,
                                      "https://notify.windows.com.evil.test/w", "https://evilnotify.windows.com.x/w",
                                      "https://notify.windows.com/w", "http://wns2-x.notify.windows.com/w",
                                      "https://wns2-x.notify.windows.com:8443/w"])
def test_subscriptions_cannot_turn_push_into_an_arbitrary_post(endpoint):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        validate_subscription({**subscription(), "endpoint": endpoint})
    assert error.value.status_code == 400


def test_edge_windows_push_address_is_accepted():
    endpoint = "https://wns2-pn1p.notify.windows.com/w/?token=abc"
    assert validate_subscription({**subscription(), "endpoint": endpoint})["endpoint"] == endpoint


def test_push_routes_are_locked_and_test_is_owner_only(world):
    queue = InlineQueue()
    app = create_app(world.record, settings=SETTINGS, model=FakeModel(), queue=queue, auth_settings=AUTH)
    app.state.alerts.settings = PUSH
    app.state.alerts.sender = world.service.sender
    client = TestClient(app)
    for path in ("/push/subscribe", "/push/unsubscribe", "/push/test"):
        assert client.post(path, json={}).status_code == 401
    client.cookies.set(SESSION_COOKIE, app.state.auth.make_cookie(world.doctor))
    assert client.post("/push/test").status_code == 403
    assert client.post("/push/subscribe", json={}).json() == {"public_key": PUSH.public}
    result = client.post("/push/subscribe", json={"subscription": subscription("api"), "device_label": "Test phone"})
    assert result.status_code == 200
    client.cookies.set(SESSION_COOKIE, app.state.auth.make_cookie(Doctor(OWNER, "Owner", "owner")))
    assert client.post("/push/test").json() == {"sent": 0, "failed": 0}
    owner_sub = subscription("owner")
    assert client.post("/push/subscribe", json={"subscription": owner_sub}).status_code == 200
    assert client.post("/push/test").json() == {"sent": 1, "failed": 0}
    assert client.post("/push/unsubscribe", json={"endpoint": owner_sub["endpoint"]}).status_code == 200
    assert client.post("/push/test").json() == {"sent": 0, "failed": 0}


def test_merge_and_report_routes_call_alerts_without_models_or_cloud(world, monkeypatch):
    queue = InlineQueue()
    app = create_app(world.record, settings=SETTINGS, model=FakeModel(), queue=queue, auth_settings=AUTH)
    queue.app = app
    app.state.alerts.settings = PUSH
    app.state.alerts.sender = world.service.sender
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.auth.make_cookie(world.doctor))
    aid = world.admission["id"]
    response = client.post(f"/admissions/{aid}/notes", json={"text": "Date 25/10\nPR 120/min"})
    assert response.status_code == 200
    assert len(world.sent) == 1 and not world.sent[0][1]["silent"]
    assert client.post(f"/admissions/{aid}/notes", json={"text": "Date 25/10\nPR 120/min"}).json()["duplicate"]
    assert len(world.sent) == 1
    monkeypatch.setattr(queue, "enqueue", _no_queue)
    files = {"file": ("report.pdf", b"%PDF-synthetic-report", "application/pdf")}
    assert client.post(f"/admissions/{aid}/reports", files=files).status_code == 200
    assert len(world.sent) == 2 and world.sent[-1][1]["silent"]
    assert client.post(f"/admissions/{aid}/reports", files=files).json()["duplicate"]
    assert len(world.sent) == 2


async def _no_queue(*args):
    pass


def test_push_failure_cannot_turn_a_successful_merge_into_a_failed_case(world, monkeypatch):
    queue = InlineQueue()
    app = create_app(world.record, settings=SETTINGS, model=FakeModel(), queue=queue, auth_settings=AUTH)
    queue.app = app
    app.state.alerts.settings = PUSH
    def fail(*args, **kwargs):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(app.state.alerts, "after_merge", fail)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.auth.make_cookie(world.doctor))
    assert client.post(f"/admissions/{world.admission['id']}/notes", json={"text": "Date 25/10\nPR 120/min"}).status_code == 200
    assert app.state.push_failures == 1


def test_public_assets_are_data_free_and_icons_are_real_pngs(world):
    app = create_app(world.record, settings=SETTINGS, auth_settings=AUTH)
    client = TestClient(app)
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and "application/manifest+json" in manifest.headers["content-type"]
    data = manifest.json()
    assert (data["name"], data["display"], data["start_url"], data["theme_color"]) == ("WardOS", "standalone", "/", "#EDF6F2")
    assert {i["purpose"] for i in data["icons"]} == {"any", "maskable"}
    for item in data["icons"]:
        response = client.get(item["src"])
        assert response.status_code == 200
        image = Image.open(io.BytesIO(response.content))
        assert image.size == tuple(int(n) for n in item["sizes"].split("x"))
    response = client.get("/sw.js")
    assert response.status_code == 200 and response.headers["service-worker-allowed"] == "/"
    assert "Synthetic Person" not in response.text + manifest.text
    assert client.get("/icons/unknown.png").status_code == 401
    assert client.get("/admissions").status_code == 401


def test_no_configuration_disables_push_without_claiming_events(world):
    world.service.settings = PushSettings()
    assert world.service.after_merge(world.admission["id"], {}, report_ids=["report"]) == {"sent": 0, "failed": 0}
    assert world.record.store.query(SENT) == []


def test_real_webpush_adapter_uses_a_timeout_and_quiet_urgency(world, monkeypatch):
    calls = []
    import pywebpush
    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: calls.append(kwargs))
    world.service._webpush(subscription(), {"title": "WardOS", "silent": True})
    assert calls[0]["timeout"] == 10 and calls[0]["headers"] == {"Urgency": "low"}
    assert calls[0]["vapid_claims"] == {"sub": PUSH.subject}
