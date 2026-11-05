"""Doctor sign-in."""
from __future__ import annotations

import re
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from wardos.chain import auth as auth_module
from wardos.chain.api import create_app
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import AuthSettings, ChainSettings
from wardos.models import Usage

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6, store="sqlite", files="local")
CLIENT_ID = "client-id-for-tests"
URL = "https://ward.example.test"
OWNER, DOCTOR, STRANGER = "owner@example.test", "anita@example.test", "stranger@example.test"
ROBOT = "runner@example.test"
AUTH = AuthSettings(client_id=CLIENT_ID, client_secret="client-secret", cookie_secret="cookie-secret-for-tests",
                    public_url=URL, owner_emails=(OWNER,), machine_accounts=(ROBOT,))


class FakeGoogle:

    def __init__(self):
        self.tokens: dict[str, tuple[str, dict]] = {}

    def issue(self, token: str, audience: str, **claims) -> str:
        self.tokens[token] = (audience, claims)
        return token

    def verify(self, token, audience):
        if token not in self.tokens or self.tokens[token][0] != audience:
            raise ValueError("wrong audience or unknown token")
        return self.tokens[token][1]

    def exchange(self, code, redirect_uri):
        assert redirect_uri == f"{URL}/auth/callback"
        return code


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class RecordingQueue:
    def __init__(self):
        self.calls = []

    async def enqueue(self, admission_id, source_ids, **_):
        self.calls.append((admission_id, source_ids))


class EmptyModel:
    async def generate(self, prompt, **kwargs):
        return {"pages": [{"p": 1, "type": "rounds_note", "date": None}], "facts": []}, Usage("fake", cost_inr=0.0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDOS_DEV_DOCTOR", raising=False)
    google, clock, queue = FakeGoogle(), Clock(), RecordingQueue()
    record = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    app = create_app(record, model=EmptyModel(), settings=SETTINGS, queue=queue, auth_settings=AUTH,
                     verify_token=google.verify, exchange_code=google.exchange, clock=clock)

    class World:
        pass
    w = World()
    w.app, w.google, w.clock, w.queue, w.record = app, google, clock, queue, record
    w.client = lambda: TestClient(app, base_url="https://testserver", follow_redirects=False)
    return w


def sign_in(world, email, name="Google Name", *, verified=True, next_path="/"):
    client = world.client()
    login = client.get("/auth/login", params={"next": next_path})
    assert login.status_code == 302
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    code = world.google.issue(f"id-{email}-{len(world.google.tokens)}", CLIENT_ID, email=email,
                              email_verified=verified, name=name)
    return client, client.get("/auth/callback", params={"code": code, "state": state})


def owner_client(world):
    client, response = sign_in(world, OWNER, "Olive Owner")
    assert response.status_code == 302
    return client


def add_doctor(world, owner, email=DOCTOR, name="Anita Rao", role="doctor"):
    response = owner.post("/doctors", json={"email": email, "name": name, "role": role})
    assert response.status_code == 200, response.text
    return response.json()


def machine_headers(world, audience=URL, email=ROBOT, verified=True):
    token = world.google.issue(f"machine-{len(world.google.tokens)}", audience, email=email, email_verified=verified)
    return {"Authorization": f"Bearer {token}"}


def test_every_route_but_the_public_ones_needs_sign_in(world):
    client = world.client()
    walked = 0
    for route in world.app.routes:
        path = getattr(route, "path", None)
        if path is None or path in auth_module.PUBLIC_PATHS:
            continue
        for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"}):
            url = re.sub(r"\{[^}]+\}", "x", path)
            response = client.request(method, url)
            assert response.status_code == 401, f"{method} {path} answered {response.status_code} without sign-in"
            walked += 1
    assert walked >= 30
    assert client.get("/no/such/route").status_code == 401
    assert client.get("/docs").status_code == 401 and client.get("/openapi.json").status_code == 401
    page = client.get("/")
    assert page.status_code == 401 and "Sign in with Google" in page.text and "Only doctors added by the ward" in page.text
    assert client.get("/mission").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/admissions", headers={"X-WardOS-Doctor": "anyone"}).status_code == 401


def test_nothing_is_configured_means_nobody_gets_in(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDOS_DEV_DOCTOR", raising=False)
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")), model=object(),
                     settings=SETTINGS, auth_settings=AuthSettings())
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert client.get("/admissions").status_code == 401
    assert client.get("/auth/login").status_code == 500


def test_login_round_trip_owner_doctor_and_stranger(world):
    login = world.client().get("/auth/login", params={"next": "https://evil.example.test/x"})
    query = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)
    assert login.headers["location"].startswith("https://accounts.google.com/")
    assert query["client_id"] == [CLIENT_ID] and query["redirect_uri"] == [f"{URL}/auth/callback"]
    assert query["scope"] == ["openid email profile"] and query["prompt"] == ["select_account"]

    owner = owner_client(world)
    assert owner.get("/auth/me").json() == {"email": OWNER, "name": "Olive Owner", "role": "owner"}
    assert owner.get("/admissions").status_code == 200
    cookie = next(h for h in owner.cookies.jar if h.name == auth_module.SESSION_COOKIE)
    assert cookie.secure and cookie.has_nonstandard_attr("HttpOnly")

    add_doctor(world, owner)
    doctor, response = sign_in(world, DOCTOR, "Google Says Anita", next_path="/tests")
    assert response.status_code == 302 and response.headers["location"] == "/tests"
    assert doctor.get("/auth/me").json() == {"email": DOCTOR, "name": "Anita Rao", "role": "doctor"}


def test_unknown_google_account_sees_the_not_on_the_list_page_and_gets_no_cookie(world):
    client, response = sign_in(world, STRANGER)
    assert response.status_code == 403
    assert f"You are signed in as {STRANGER}, but you are not on this ward" in response.text
    assert "Use another account" in response.text and "Ask the owner to add you" in response.text
    assert client.cookies.get(auth_module.SESSION_COOKIE) is None
    assert client.get("/auth/me").status_code == 401 and client.get("/admissions").status_code == 401


def test_unverified_email_and_a_replayed_or_forged_state_are_refused(world):
    client, response = sign_in(world, OWNER, verified=False)
    assert response.status_code == 400 and client.get("/auth/me").status_code == 401

    fresh = world.client()
    login = world.client().get("/auth/login")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    world.google.issue("t", CLIENT_ID, email=OWNER, email_verified=True, name="x")
    assert fresh.get("/auth/callback", params={"code": "t", "state": state}).status_code == 400
    assert fresh.get("/auth/callback", params={"code": "t", "state": "forged"}).status_code == 400
    assert fresh.get("/auth/callback", params={"error": "access_denied"}).status_code == 400
    assert fresh.get("/auth/me").status_code == 401


def test_sign_in_link_expires_after_ten_minutes(world):
    client = world.client()
    login = client.get("/auth/login")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(login.headers["location"]).query)["state"][0]
    world.google.issue("t", CLIENT_ID, email=OWNER, email_verified=True, name="x")
    world.clock.now += 601
    assert client.get("/auth/callback", params={"code": "t", "state": state}).status_code == 400


def test_a_removed_doctor_is_out_within_the_cache_time(world):
    owner = owner_client(world)
    add_doctor(world, owner)
    doctor, _ = sign_in(world, DOCTOR)
    assert doctor.get("/admissions").status_code == 200
    row = world.record.store.get(auth_module.DOCTORS, DOCTOR)
    world.record.store.put(auth_module.DOCTORS, {**row, "active": False})
    world.clock.now += 30
    assert doctor.get("/admissions").status_code == 200
    world.clock.now += 31
    assert doctor.get("/admissions").status_code == 401
    assert sign_in(world, DOCTOR)[1].status_code == 403


def test_removing_through_the_owner_screen_works_at_once(world):
    owner = owner_client(world)
    add_doctor(world, owner)
    doctor, _ = sign_in(world, DOCTOR)
    assert doctor.get("/admissions").status_code == 200
    assert owner.post(f"/doctors/{DOCTOR}/remove").json()["active"] is False
    assert doctor.get("/admissions").status_code == 401
    rows = {r["email"]: r for r in owner.get("/doctors").json()}
    assert rows[DOCTOR]["active"] is False and rows[OWNER]["fixed"] is True


def test_tampered_foreign_and_expired_cookies_are_refused(world):
    owner = owner_client(world)
    good = owner.cookies.get(auth_module.SESSION_COOKIE)
    other = auth_module.URLSafeSerializer("another-secret", salt="wardos-session").dumps(
        {"e": OWNER, "n": "x", "iat": world.clock()})
    for bad in (good[:-2] + ("AA" if not good.endswith("AA") else "BB"), "garbage", other, ""):
        client = world.client()
        client.cookies.set(auth_module.SESSION_COOKIE, bad)
        assert client.get("/admissions").status_code == 401, bad
    still = world.client()
    still.cookies.set(auth_module.SESSION_COOKIE, good)
    world.clock.now += 29 * 24 * 3600
    assert still.get("/admissions").status_code == 200
    world.clock.now += 2 * 24 * 3600
    assert still.get("/admissions").status_code == 401


def test_logout_clears_the_cookie(world):
    owner = owner_client(world)
    assert owner.get("/auth/logout").status_code == 302
    assert owner.get("/auth/me").status_code == 401


def test_internal_process_takes_only_a_good_service_account_token(world):
    owner = owner_client(world)
    patient = owner.post("/patients", json={"name": "Test Patient"}).json()
    admission = owner.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    note = owner.post(f"/admissions/{admission}/notes", json={"text": "Date 20/10\nPR 88/min"}).json()["source_id"]
    body = {"admission_id": admission, "source_ids": [note]}

    anonymous = world.client()
    assert anonymous.post("/internal/process", json=body).status_code == 401
    assert owner.post("/internal/process", json=body).status_code == 401
    assert anonymous.post("/internal/process", json=body, headers={"Authorization": "Bearer nonsense"}).status_code == 403
    wrong_audience = machine_headers(world, audience="https://elsewhere.example.test")
    assert anonymous.post("/internal/process", json=body, headers=wrong_audience).status_code == 403
    unlisted = machine_headers(world, email="intruder@example.test")
    assert anonymous.post("/internal/process", json=body, headers=unlisted).status_code == 403
    unverified = machine_headers(world, verified=False)
    assert anonymous.post("/internal/process", json=body, headers=unverified).status_code == 403
    good = anonymous.post("/internal/process", json=body, headers=machine_headers(world))
    assert good.status_code == 200 and good.json()["sources"] == 1
    assert anonymous.get("/admissions", headers=machine_headers(world)).status_code == 401


def test_fetched_reports_accept_a_doctor_or_a_machine_token(world):
    owner = owner_client(world)
    patient = owner.post("/patients", json={"name": "Test Patient"}).json()
    admission = owner.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    pdf = {"file": ("lab.pdf", b"%PDF-1.4 one", "application/pdf")}
    anonymous = world.client()
    assert anonymous.post(f"/admissions/{admission}/reports", files=pdf).status_code == 401
    assert anonymous.post(f"/admissions/{admission}/reports", files=pdf,
                          headers={"Authorization": "Bearer nonsense"}).status_code == 403
    robot = anonymous.post(f"/admissions/{admission}/reports", files=pdf, headers=machine_headers(world))
    assert robot.status_code == 200
    human = owner.post(f"/admissions/{admission}/reports", files={"file": ("lab.pdf", b"%PDF-1.4 two", "application/pdf")})
    assert human.status_code == 200
    authors = {s["id"]: s["author"] for s in world.record.store.query("sources", admission_id=admission)}
    assert authors[robot.json()["source_id"]] == "hospital-agent" and authors[human.json()["source_id"]] == "Olive Owner"


def test_only_the_owner_manages_doctors(world):
    owner = owner_client(world)
    added = add_doctor(world, owner)
    assert added["added_by"] == OWNER and added["active"] is True and added["id"] == DOCTOR
    doctor, _ = sign_in(world, DOCTOR)
    assert doctor.get("/doctors").status_code == 403
    assert doctor.post("/doctors", json={"email": STRANGER, "name": "X"}).status_code == 403
    assert doctor.post(f"/doctors/{DOCTOR}/remove").status_code == 403
    assert owner.post("/doctors", json={"email": "not-an-email", "name": "X"}).status_code == 400
    assert owner.post("/doctors", json={"email": STRANGER, "name": "  "}).status_code == 400
    assert owner.post("/doctors", json={"email": STRANGER, "name": "X", "role": "king"}).status_code == 400
    assert owner.post("/doctors", json={"email": OWNER, "name": "X"}).status_code == 400
    assert owner.post(f"/doctors/{OWNER}/remove").status_code == 400
    assert owner.post("/doctors/nobody@example.test/remove").status_code == 404
    second = add_doctor(world, owner, "second@example.test", "Second Owner", "owner")
    boss, _ = sign_in(world, "second@example.test")
    assert second["role"] == "owner" and boss.get("/doctors").status_code == 200
    assert boss.post("/doctors/second@example.test/remove").status_code == 400
    assert owner.post("/doctors/second@example.test/remove").status_code == 200


def test_the_record_gets_the_signed_in_name_not_the_header(world):
    owner = owner_client(world)
    add_doctor(world, owner)
    doctor, _ = sign_in(world, DOCTOR)
    patient = doctor.post("/patients", json={"name": "Test Patient"}).json()
    admission = doctor.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    note = doctor.post(f"/admissions/{admission}/notes", json={"text": "Date 20/10\nPR 88/min"},
                       headers={"X-WardOS-Doctor": "Mallory"}).json()
    source = world.record.store.get("sources", note["source_id"])
    assert source["author"] == "Anita Rao"
    severity = doctor.patch(f"/admissions/{admission}/severity", json={"severity": "watch"},
                            headers={"X-WardOS-Doctor": "Mallory"}).json()
    assert severity["severity_by"] == "Anita Rao"
    assert world.queue.calls


def test_dev_doctor_on_cloud_run_refuses_to_start(tmp_path, monkeypatch):
    record = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    with pytest.raises(RuntimeError, match="WARDOS_DEV_DOCTOR"):
        create_app(record, model=object(), settings=SETTINGS,
                   auth_settings=AuthSettings(dev_doctor="Test Doctor", on_cloud_run=True))
    monkeypatch.setenv("WARDOS_DEV_DOCTOR", "Test Doctor")
    monkeypatch.setenv("K_SERVICE", "wardos-chain")
    with pytest.raises(RuntimeError, match="K_SERVICE"):
        create_app(record, model=object(), settings=SETTINGS)


def test_dev_doctor_alone_signs_in_locally_and_still_ignores_the_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDOS_DEV_DOCTOR", "Test Doctor")
    monkeypatch.delenv("K_SERVICE", raising=False)
    queue = RecordingQueue()
    app = create_app(Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f")), model=object(),
                     settings=SETTINGS, queue=queue)
    client = TestClient(app)
    assert client.get("/auth/me").json()["name"] == "Test Doctor" and client.get("/").status_code == 200
    patient = client.post("/patients", json={"name": "Test Patient"}).json()
    admission = client.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2"}).json()["id"]
    note = client.post(f"/admissions/{admission}/notes", json={"text": "hello"}, headers={"X-WardOS-Doctor": "Mallory"}).json()
    assert app.state.record.store.get("sources", note["source_id"])["author"] == "Test Doctor"
    assert client.post("/internal/process", json={"admission_id": admission, "source_ids": []}).status_code == 401
