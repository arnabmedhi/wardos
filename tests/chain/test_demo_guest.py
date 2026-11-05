"""A guest sees only their own demo copy, on every route."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from wardos.chain import model as m
from wardos.chain.api import create_app
from wardos.chain.auth import Doctor, SESSION_COOKIE
from wardos.chain.demo import GUESTS, GUEST_COOKIE, DemoAskModel, DemoSite, make_copy, status_page, watch_alert
from wardos.chain.docstore import SQLiteDocStore
from wardos.hospital.demo_record import guest_record, ingest
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import AuthSettings, load_chain_settings

REAL = "REALPATIENTMARKER"
ROOT = Path(__file__).resolve().parents[2]
JPEG = b"\xff\xd8\xff\xe0demo-photo-bytes"


def stay(bed, name):
    aid, pid, sid, gid, fid = (f"{p}_{int(bed):016x}" for p in ("adm", "pat", "src", "page", "fact"))
    return {"patient": {"id": pid, "name": name, "uhid": f"2026{bed}", "created_at": "2026-10-01T00:00:00+00:00"},
            "admission": {"id": aid, "patient_id": pid, "unit": "Medicine", "status": "active", "bed": bed, "demo_bed": bed,
                          "version": 1, "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00"},
            "sources": [{"id": sid, "admission_id": aid, "kind": "photo", "mime": "image/jpeg", "status": "merged",
                         "file_uri": f"demo-photo:{sid}.jpg", "received_at": "2026-10-01T00:00:00+00:00"}],
            "pages": [{"id": gid, "admission_id": aid, "source_id": sid, "number": 1, "text": f"{name} potassium 3.1"}],
            "facts": [m.fact_doc(admission_id=aid, source_id=sid, page_id=gid, index=0, type="lab_result", key="potassium",
                                 label="Potassium", value={"text": "3.1", "number": 3.1, "unit": "mmol/L"},
                                 clinical_time="2026-10-11", time_precision="day", assertion="present", quote="3.1", quote_found=True)
                      | {"id": fid, "added_in": 1}],
            "questions": [], "events": [], "held_facts": []}


def reading(said: str) -> dict:
    """What a fake Muse says for a command (the real one reads any wording)."""
    words = said.lower()
    bed = re.search(r"bed\s*(\d+)", words)
    test = next((t for t in ("sodium", "potassium", "creatinine", "hemoglobin") if t in words), None)
    kind = ("stop_watch" if "stop watching" in words else "list_watches" if "watching" in words and "what" in words
            else "watch" if "watch" in words else "get_reports" if "report" in words
            else "hospital_question" if any(k in words for k in ("free beds", "admitted", "vacant")) and "bed 1" not in words
            else "cannot" if any(k in words for k in ("order", "send bed", "give ")) else "record_question")
    text = "I can't do that yet" if kind == "cannot" else said if kind in ("hospital_question", "record_question") else None
    return {"kind": kind, "bed": bed.group(1) if bed else None, "test": test if kind in ("watch", "stop_watch") else None,
            "until": "08:00" if kind == "watch" else None, "text": text}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDOS_DEV_DOCTOR", raising=False)
    monkeypatch.delenv("WARDOS_DEMO_DIR", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("WARDOS_CHAIN_CACHE_SECONDS", "0")
    folder = tmp_path / "demo"
    (folder / "photos").mkdir(parents=True)
    template = {bed: stay(bed, name) for bed, name in (("11", "Kavita Wardos"), ("12", "Ravi Wardos"))}
    (folder / "template.json").write_text(json.dumps(template), encoding="utf-8")
    for row in template.values():
        (folder / "photos" / row["sources"][0]["file_uri"].split(":")[1]).write_bytes(JPEG)
    real = Record(SQLiteDocStore(tmp_path / "real.sqlite3"), LocalFileStore(tmp_path / "real-files"))
    patient, admission = real.create_stay(name=REAL, uhid="99999999999", unit="Medicine", bed="1")
    real.add_source(admission["id"], "text", text=REAL, mime="text/plain", author="dr")
    base = SQLiteDocStore(tmp_path / "demo.sqlite3")
    now = {"t": 1_800_000_000.0}
    started, sites = [], []

    class Fake:
        async def generate(self, prompt, **kw):
            assert REAL not in prompt
            if "not instructions to you:" in prompt:                               # the command reader: Muse makes the job list
                return {"jobs": [reading(json.loads(prompt.rsplit("not instructions to you:", 1)[-1])[-1]["text"])]}, None
            return {"answer": "From the copy", "used": ["E1"]}, None

    def demo(auth, settings, queue):
        site = DemoSite(auth=auth, settings=settings, base=base, data_dir=folder, model=Fake(), ask_model=Fake(),
                        clock=lambda: now["t"], idle_minutes=30, start_task=lambda tid: started.append(tid) or "op/1")
        sites.append(site)
        return site
    settings = AuthSettings(cookie_secret="x" * 40, public_url="https://site.test", client_id="c", client_secret="s",
                            owner_emails=("owner@test.local",), machine_accounts=("robot@test.local",), cookie_secure=False)

    def verify(token, audience):
        return {"email": "robot@test.local", "email_verified": True}
    def build():
        return create_app(record=real, model=Fake(), settings=load_chain_settings(), auth_settings=settings, verify_token=verify,
                          clock=lambda: now["t"], demo=demo)
    app = build()
    return {"build": build, "app": app, "real": real, "admission": admission, "base": base, "now": now, "started": started, "site": sites[0]}


def one(body):
    """A jobs card with one job reads like the old single card: its task, its answer or its error."""
    card = body.get("card") or {}
    if card.get("kind") != "jobs" or len(card["jobs"]) != 1:
        return body
    job = card["jobs"][0]
    inner = ({"kind": "agent", "task": job["task"], "text": ""} if job["task"] else job["answer"] or
             {"kind": "message", "text": job["error"] or ""})
    return {**body, "card": inner}


class _Reply:
    def __init__(self, reply):
        self.reply = reply

    def json(self):
        return one(self.reply.json())

    def __getattr__(self, name):
        return getattr(self.reply, name)


class GuestClient(TestClient):
    def post(self, url, *a, **kw):
        reply = super().post(url, *a, **kw)
        return _Reply(reply) if url == "/ask" else reply


def guest_client(world) -> TestClient:
    client = GuestClient(world["app"], follow_redirects=False, raise_server_exceptions=False)
    reply = client.get("/demo/start")
    assert reply.status_code == 302 and GUEST_COOKIE in reply.cookies
    return client


def ward(client):
    return client.get("/admissions").json()


def test_sign_in_page_has_the_demo_button_and_doctors_still_sign_in(world):
    page = TestClient(world["app"]).get("/").text
    assert "Try the demo (no sign-in)" in page and "/auth/login" in page


def test_guest_sees_the_two_and_no_real_data(world):
    client = guest_client(world)
    rows = ward(client)
    assert sorted(r["row"]["bed"] for r in rows) == ["11", "12"]
    assert REAL not in json.dumps(rows)
    assert client.get("/auth/me").json()["demo"] is True


def test_every_route_shows_a_guest_no_real_data(world):
    client = guest_client(world)
    ward(client)
    real_id = world["admission"]["id"]
    real_source = world["real"].store.query(m.SOURCES, admission_id=real_id)[0]["id"]
    seen = 0
    for route in world["app"].routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            path = route.path
            for name in re.findall(r"{(\w+)}", path):
                path = path.replace("{" + name + "}", {"source_id": real_source, "fact_id": "fact_x", "question_id": "q_x",
                                                       "run_id": "run", "email": "owner@test.local", "name": "icon-192.png",
                                                       "task_id": "task_x"}.get(name, real_id))
            body = {"text": "potassium", "admission_id": real_id, "patient_id": real_id, "unit": "x", "name": "n", "bed": "1",
                    "action": "confirm", "answer": "a", "severity": "stable", "email": "a@b.co", "line": "x", "guest": "g", "source_ids": []}
            reply = client.request(method, path, json=body)
            seen += 1
            assert REAL not in reply.text, (method, route.path)
            assert "99999999999" not in reply.text, (method, route.path)
            if reply.status_code == 200 and "/admissions/" in path and method == "GET":
                pytest.fail(f"guest opened a real stay: {route.path}")
    assert seen > 40
    assert world["real"].store.get(m.PATIENTS, world["real"].store.query(m.PATIENTS)[0]["id"])["name"] == REAL
    assert len(world["real"].store.query(m.ADMISSIONS)) == 1


@pytest.mark.parametrize("method,path", [("POST", "/register/find"), ("POST", "/register/run/pull-in"), ("GET", "/doctors"),
                                         ("POST", "/push/test"),
                                         ("POST", "/internal/process"), ("POST", "/internal/demo-alert"),
                                         ("POST", "/agent/worker/next"), ("POST", "/admissions/adm_x/final"),
                                         ("GET", "/admissions/adm_x/final")])
def test_guest_gets_a_plain_403(world, method, path):
    client = guest_client(world)
    reply = client.request(method, path, json={"text": "x"})
    assert reply.status_code == 403


def test_two_guests_never_see_each_other(world):
    one, two = guest_client(world), guest_client(world)
    a, b = ward(one), ward(two)
    assert {r["admission"]["id"] for r in a}.isdisjoint(r["admission"]["id"] for r in b)
    assert one.get(f"/admissions/{b[0]['admission']['id']}/today").status_code == 404
    one.patch(f"/admissions/{a[0]['admission']['id']}/severity", json={"severity": "unstable"})
    assert all(r["row"]["severity"] is None for r in ward(two))


def test_a_guest_does_things_on_their_own_copy(world):
    client = guest_client(world)
    row = ward(client)[0]
    aid = row["admission"]["id"]
    assert client.patch(f"/admissions/{aid}", json={"bed": "77"}).status_code == 200
    made = client.post("/patients/admit", json={"name": "New One", "unit": "Medicine", "bed": "30"})
    assert made.status_code == 200
    assert len(ward(client)) == 3
    assert len(world["real"].store.query(m.ADMISSIONS)) == 1
    fact = client.get(f"/admissions/{aid}/facts").json()[0]
    photo = client.get(f"/sources/{fact['source_id']}/file")
    assert photo.status_code == 200 and photo.content == JPEG


def test_a_guest_upload_stays_in_the_guest_copy(world):
    client = guest_client(world)
    aid = ward(client)[0]["admission"]["id"]
    reply = client.post(f"/admissions/{aid}/uploads", files={"files": ("p.jpg", b"\xff\xd8\xff" + b"z" * 900_000, "image/jpeg")})
    sid = reply.json()["files"][0]["source_id"]
    assert client.get(f"/sources/{sid}/file").content.endswith(b"z" * 10)
    assert world["base"].query("guest_files")[0]["guest"]


def test_copy_resets_after_idle_and_not_while_in_use(world):
    client = guest_client(world)
    first = ward(client)
    aid = first[0]["admission"]["id"]
    client.patch(f"/admissions/{aid}", json={"bed": "77"})
    world["now"]["t"] += 29 * 60
    assert {r["admission"]["id"] for r in ward(client)} == {r["admission"]["id"] for r in first}
    world["now"]["t"] += 29 * 60
    assert {r["admission"]["id"] for r in ward(client)} == {r["admission"]["id"] for r in first}
    world["now"]["t"] += 31 * 60
    reply = client.get("/admissions")
    assert reply.headers.get("x-wardos-demo-reset") == "1"
    fresh = reply.json()
    assert {r["admission"]["id"] for r in fresh}.isdisjoint(r["admission"]["id"] for r in first)
    assert sorted(r["row"]["bed"] for r in fresh) == ["11", "12"]
    assert world["base"].get(m.ADMISSIONS, aid) is None


def test_no_reset_while_the_robot_works(world):
    client = guest_client(world)
    first = ward(client)
    aid = first[0]["admission"]["id"]
    client.post("/ask", json={"text": "get the reports", "admission_id": aid})
    world["now"]["t"] += 31 * 60
    task = world["base"].query("tasks")[0]
    world["base"].put("tasks", {**task, "status": "working", "updated_at": datetime.fromtimestamp(world["now"]["t"], timezone.utc).isoformat()})
    assert {r["admission"]["id"] for r in ward(client)} == {r["admission"]["id"] for r in first}


def test_get_the_reports_makes_one_task_and_starts_the_robot(world):
    client = guest_client(world)
    row = next(r for r in ward(client) if r["row"]["bed"] == "12")
    aid = row["admission"]["id"]
    reply = client.post("/ask", json={"text": "get the reports", "admission_id": aid}).json()
    assert reply["card"]["kind"] == "agent" and reply["card"]["task"]["active"]
    task = world["base"].query("tasks")[0]
    assert (task["bed"], task["kind"], task["status"], task["admission_id"]) == ("12", "get_reports", "waiting", aid)
    assert world["started"] == [task["id"]] and task["operation"] == "op/1"
    client.post("/ask", json={"text": "get the reports", "admission_id": aid})
    assert len(world["base"].query("tasks")) == 1
    assert client.get(f"/agent/tasks/{task['id']}").json()["id"] == task["id"]
    other = guest_client(world)
    ward(other)
    assert other.get(f"/agent/tasks/{task['id']}").status_code == 404


def test_watch_any_ordinary_test_on_any_demo_bed(world):
    client = guest_client(world)
    rows = {r["row"]["bed"]: r["admission"]["id"] for r in ward(client)}
    other = client.post("/ask", json={"text": "watch bed 11's sodium"}).json()
    assert other["card"]["kind"] == "agent" and other["card"]["task"]["title"] == "Watching bed 11: sodium"
    right = client.post("/ask", json={"text": "watch potassium", "admission_id": rows["12"]}).json()
    assert right["card"]["kind"] == "agent"
    tasks = {(t["bed"], t["test"]) for t in world["base"].query("tasks") if t["kind"] == "watch"}
    assert tasks == {("11", "sodium"), ("12", "potassium")}
    nameless = client.post("/ask", json={"text": "watch bed 11", "admission_id": rows["11"]}).json()
    assert nameless["card"]["kind"] == "message" and "Which test" in nameless["card"]["text"]


DANGER_LINE = ("DANGER · Serum potassium 2.6 mmol/L · normal 3.5 – 5.6 mmol/L · collected 05/11 03:16 · "
               "earlier: 2.8 (29/10 20:14), 3.4 (27/10 22:40), 4.9 (25/10 08:53)")


def test_a_watch_alert_has_the_shape_of_a_real_one():
    alert = watch_alert("potassium", ["Signed in", "checked · no new potassium", DANGER_LINE, "checked · no new potassium"])
    assert alert == {"test": "potassium", "key": "potassium", "label": "Serum potassium", "value": "2.6", "unit": "mmol/L",
                     "normal": "3.5 – 5.6 mmol/L", "time": "05/11 03:16",
                     "earlier": [{"value": "2.8", "time": "29/10 20:14"}, {"value": "3.4", "time": "27/10 22:40"},
                                 {"value": "4.9", "time": "25/10 08:53"}]}
    assert watch_alert("potassium", ["checked · no new potassium"]) is None
    first = watch_alert("potassium", ["DANGER · Serum potassium 2.6 mmol/L · collected 05/11 03:16 · earlier: none"])
    assert first["value"] == "2.6" and first["earlier"] == [] and first["normal"] is None


def test_a_watch_alert_empties_the_cache_on_every_server_copy(world, monkeypatch):
    monkeypatch.setenv("WARDOS_CHAIN_CACHE_SECONDS", "300")
    client = guest_client(world)
    aid = next(r for r in ward(client) if r["row"]["bed"] == "12")["admission"]["id"]
    task = client.post("/ask", json={"text": "watch potassium", "admission_id": aid}).json()["card"]["task"]
    guest = world["base"].get("tasks", task["id"])["guest"]
    shown = lambda: [r["value"] for r in client.get(f"/admissions/{aid}/today").json()["labs"] if r["key"] == "potassium"]
    before = shown()
    assert before == ["3.1"]
    record = guest_record(world["base"], guest)
    when = datetime.fromtimestamp(world["now"]["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M")
    ingest(record, world["base"].get("tasks", task["id"]), [SimpleNamespace(
        analyte="Serum potassium", value="2.6", number=2.6, unit="mmol/L", reference="3.5 – 5.6 mmol/L", collected_at=when, sample_no="s1")])
    assert shown() == before                                                    # the cache does not know yet
    world["base"].put("tasks", {**world["base"].get("tasks", task["id"]), "steps": [DANGER_LINE]})
    assert client.get(f"/agent/tasks/{task['id']}").json()["alert"]["key"] == "potassium"
    token = client.cookies.get("wardos_gw")
    reply = client.post(f"/agent/tasks/{task['id']}/fresh")
    assert reply.status_code == 200 and reply.json() == {"ok": True}
    assert client.cookies.get("wardos_gw") != token                             # the other servers empty theirs on the next read
    assert shown() == ["2.6"]
    other = guest_client(world)
    assert other.post(f"/agent/tasks/{task['id']}/fresh").status_code == 404    # another guest cannot touch it


def test_the_alert_page_opens_the_alert_of_its_watch():
    page = (ROOT / "wardos" / "chain" / "static" / "index.html").read_text(encoding="utf-8")
    assert "spec: `alert:${v.id}`" in page and 'data-go="alert:${esc(id)}"' in page
    assert "alert(taskId) {" in page and "alertArrived(v);" in page


def test_robot_start_failure_shows_failed(world):
    client = guest_client(world)
    world["site"].start_task = lambda tid: (_ for _ in ()).throw(RuntimeError("no"))
    aid = next(r for r in ward(client) if r["row"]["bed"] == "12")["admission"]["id"]
    assert client.post("/ask", json={"text": "get the reports", "admission_id": aid}).json()["card"]["task"]["status"] == "failed"


def test_status_page_is_open_and_nameless(world):
    world["base"].put("status", {"id": "openmrs", "checked_at": "2026-10-30T09:00:00", "result": "Put back",
                                 "beds": [{"bed": "12", "reports": 8, "ok": True, "name": "SECRET"}]})
    page = TestClient(world["app"]).get("/demo/status")
    assert page.status_code == 200 and "Put back" in page.text and "Bed 12" in page.text and "SECRET" not in page.text
    assert "Kavita" not in page.text and REAL not in page.text
    assert "<script" not in status_page({"result": "<script>"}, None)


def test_machine_routes_refuse_everyone_else(world):
    plain = TestClient(world["app"])
    assert plain.post("/internal/demo-alert", json={"line": "x"}).status_code == 401
    good = plain.post("/internal/demo-alert", json={"line": "Put back at 14:30"}, headers={"Authorization": "Bearer t"})
    assert good.status_code == 200


def test_a_signed_in_doctor_is_never_a_guest(world):
    client = guest_client(world)
    auth = world["app"].state.auth
    client.cookies.set(SESSION_COOKIE, auth.make_cookie(Doctor("owner@test.local", "Owner", "owner")))
    assert any(r["patient"].get("name") == REAL for r in client.get("/admissions").json())


def test_forged_guest_cookie_is_refused(world):
    client = TestClient(world["app"])
    client.cookies.set(GUEST_COOKIE, "forged.value")
    assert client.get("/admissions").status_code == 401


def test_no_demo_without_a_template(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDOS_DEMO_DIR", raising=False)
    real = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    app = create_app(record=real, settings=load_chain_settings(), auth_settings=AuthSettings(cookie_secret="x" * 40, cookie_secure=False))
    client = TestClient(app)
    assert "Try the demo" not in client.get("/").text and client.get("/demo/start").status_code == 404


def test_copy_ids_are_fresh_per_guest_and_reset_and_references_follow():
    template = {"12": stay("12", "Ravi Wardos")}
    a, b, c = make_copy(template, "g1", 1), make_copy(template, "g1", 2), make_copy(template, "g2", 1)
    ids = lambda pairs: {d["id"] for _, d in pairs}
    assert not ids(a) & ids(b) and not ids(a) & ids(c)
    assert all(d["guest"] == "g1" for _, d in a)
    fact = next(d for col, d in a if col == m.FACTS)
    assert fact["source_id"] in ids(a) and fact["admission_id"] in ids(a)
    source = next(d for col, d in a if col == m.SOURCES)
    assert source["file_uri"] == template["12"]["sources"][0]["file_uri"]


@pytest.mark.asyncio
async def test_ask_uses_the_one_model_and_has_no_fallback():
    calls = []

    class Model:
        def __init__(self, fail):
            self.fail = fail

        async def generate(self, prompt, **kw):
            calls.append(kw)
            if self.fail:
                raise RuntimeError("busy")
            return {"answer": "muse"}, None
    assert (await DemoAskModel(Model(False)).generate("q", model="m", timeout=40))[0]["answer"] == "muse"
    with pytest.raises(RuntimeError, match="busy"):
        await DemoAskModel(Model(True)).generate("q", model="m")
    assert len(calls) == 2 and calls[0]["timeout"] == 60 and "model" not in calls[0]


def test_no_real_name_or_phone_in_the_real_template():
    template, hand = ROOT / "private/demo/template.json", ROOT / "private/demo/names_by_hand.json"
    if not template.is_file() or not hand.is_file():
        pytest.skip("no private demo data here")
    sys.path.insert(0, str(ROOT / "tools"))
    import demo_template
    names = json.loads(hand.read_text(encoding="utf-8-sig"))
    text = json.dumps(json.loads(template.read_text(encoding="utf-8")), ensure_ascii=False).replace("\\n", " ")
    assert demo_template.names_left(text, names) == 0
    assert not demo_template.PHONE.findall(text)
    data = json.loads(template.read_text(encoding="utf-8"))
    assert {bed: row["patient"]["name"].split()[0] for bed, row in data.items()} == {
        "11": "Harish", "12": "Meera", "13": "Gopal", "14": "Dinesh", "15": "Mohan"}
    assert all(not s.get("held_reason") and s["kind"] != "voice" for row in data.values() for s in row["sources"])
    assert all((ROOT / "private/demo/photos" / s["file_uri"].split(":")[1]).is_file() for row in data.values()
               for s in row["sources"] if s.get("file_uri"))


@pytest.mark.parametrize("words", ["order a CT for bed 12", "send bed 12 home", "give potassium to bed 12", "stop watching bed 12",
                                   "what are you watching"])
def test_only_get_reports_or_watch_starts_the_robot(world, words):
    client = guest_client(world)
    ward(client)
    card = client.post("/ask", json={"text": words}).json()["card"]
    assert card["kind"] == "message" and world["started"] == [] and not world["base"].query("tasks")


@pytest.mark.parametrize("words", ["how many free beds are there", "how many patients admitted in Surgery",
                                   "who was admitted today", "free beds in Surgery"])
def test_a_hospital_question_becomes_a_ward_task_for_the_robot(world, words):
    client = guest_client(world)
    ward(client)
    reply = client.post("/ask", json={"text": words}).json()
    task = world["base"].query("tasks")[0]
    assert reply["card"]["kind"] == "agent" and reply["card"]["task"]["kind"] == "hospital_question"
    assert (task["kind"], task["status"], task["question"]) == ("hospital_question", "waiting", words)
    assert world["started"] == [task["id"]]
    row = reply["card"]["task"]
    assert row["job"] == "Hospital question" and row["patient_label"] == "Ward" and row["time"]
    client.post("/ask", json={"text": words})
    assert len(world["base"].query("tasks")) == 1
    listed = client.get("/agent/tasks").json()["tasks"]
    assert [t["id"] for t in listed] == [task["id"]]


def test_what_are_you_watching_says_so(world):
    client = guest_client(world)
    rows = {r["row"]["bed"]: r["admission"]["id"] for r in ward(client)}
    assert "not watching" in client.post("/ask", json={"text": "what are you watching"}).json()["card"]["text"]
    client.post("/ask", json={"text": "watch potassium", "admission_id": rows["12"]})
    assert "bed 12's potassium" in client.post("/ask", json={"text": "what are you watching"}).json()["card"]["text"]


def test_photo_for_that_is_the_value_just_answered(world):
    client = guest_client(world)
    rows = {r["row"]["bed"]: r["admission"]["id"] for r in ward(client)}
    empty = client.post("/ask", json={"text": "show me the photo for that", "admission_id": rows["12"]}).json()["card"]
    assert empty["kind"] == "message" and "Which value" in empty["text"]
    client.post("/ask", json={"text": "potassium of bed 12"})
    card = client.post("/ask", json={"text": "show me the photo for that", "admission_id": rows["12"]}).json()["card"]
    assert card["kind"] == "photo" and card["row"]["label"].lower().startswith("potassium") and card["row"]["source_id"]


def test_a_patient_added_on_one_server_copy_shows_on_the_other(world, monkeypatch):
    monkeypatch.setenv("WARDOS_CHAIN_CACHE_SECONDS", "10")
    one = guest_client(world)
    two = TestClient(world["build"](), follow_redirects=False, raise_server_exceptions=False)

    def on_two():
        two.cookies.update(one.cookies)
        return len(ward(two))
    assert len(ward(one)) == 2 and on_two() == 2
    assert one.post("/patients/admit", json={"name": "New One", "unit": "Medicine", "bed": "30"}).status_code == 200
    assert len(ward(one)) == 3
    assert on_two() == 3


def test_a_ready_made_ward_is_handed_to_a_new_visitor_without_rebuilding(world, monkeypatch):
    from wardos.chain import demo as demo_module
    monkeypatch.setattr(demo_module, "WARM_TARGET", 2)
    site = world["site"]
    assert site.claim_warm() is None
    assert site.top_up() == 2 and len(site._fresh_pool()) == 2
    waiting = {r["id"] for r in site._fresh_pool()}
    assert site.top_up() == 0                                                      # already two waiting
    client = TestClient(world["app"], follow_redirects=False)
    reply = client.get("/demo/start")
    assert reply.status_code == 302
    cookie = site._cookie.loads(reply.cookies[GUEST_COOKIE])
    assert cookie["g"] in waiting                                                  # the visitor got a ready ward
    rows = TestClient(world["app"]).get("/admissions", cookies={GUEST_COOKIE: reply.cookies[GUEST_COOKIE]}).json()
    assert len(rows) == 2 and site.base.get(GUESTS, cookie["g"])["generation"] == 1
    assert cookie["g"] not in {r["id"] for r in site._fresh_pool()}                # that one is taken; a new one is built behind it
