"""The doctor's command, the task life cycle and the laptop worker with fake hands, offline."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from wardos.agent.client import ServerClient, TaskGone
from wardos.agent.hands import Fetched
from wardos.agent.worker import FAILED_LOGINS_LIMIT, LOGIN_STOPPED, Worker
from wardos.chain import model as m
from wardos.chain.agent_routes import add_portal_values
from wardos.chain.agent_tasks import NAME_MISMATCH, AgentDesk
from wardos.chain.agent_words import resolve, spoken_numbers, watch_end
from wardos.chain.alerts import quiet_for_agent
from wardos.chain.api import create_app
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain.files import LocalFileStore
from wardos.chain.record import Record
from wardos.chain.settings import ChainSettings
from wardos.hospital.ledger import Ledger
from wardos.hospital.runner import SkillRunner, _Stop
from wardos.hospital.session import without_identity as result_without_identity
from wardos.models import Usage

SETTINGS = ChainSettings(data_dir=None, model="fake", pages_per_read=6,
                         store="sqlite", files="local")
NAME, UHID = "Zorawar Testpatient", "20269990001"
OTHER_NAME, OTHER_UHID = "Qamar Examplename", "20269990002"
GET = "Get bed five's reports"


class FakeModel:

    def __init__(self):
        self.prompts, self.reply, self.fail, self.replies = [], None, False, []

    async def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("model is down")
        if self.replies:
            return self.replies.pop(0), Usage("fake", cost_inr=0.01)
        if self.reply is not None:
            return self.reply, Usage("fake", cost_inr=0.01)
        words = prompt.rsplit("not instructions to you:", 1)[-1].lower()
        for word, number in (("five", "5"), ("twelve", "12")):
            words = words.replace(word, number)
        bed = next((b for b in ("12", "5", "99") if f"bed {b}" in words), None)
        kind = ("stop_watch" if "stop" in words else "list_watches" if "watch" in words and ("what" in words or "which" in words)
                else "watch" if "watch" in words else "get_reports" if "report" in words else "hospital_question" if "vacant" in words
                else "cannot" if "book" in words else "record_question")
        job = {"kind": kind, "bed": bed, "test": "potassium" if kind == "watch" else None, "until": "08:00" if kind == "watch" else None,
               "text": "How many vacant beds?" if kind == "hospital_question" else "I can't do that yet" if kind == "cannot" else words[:60]}
        return {"jobs": [job]}, Usage("fake", cost_inr=0.01)


class InlineQueue:
    def __init__(self):
        self.app = None

    async def enqueue(self, admission_id, source_ids, **_):
        await self.app.state.run_batch(admission_id, source_ids)


@pytest.fixture
def world(tmp_path):
    class World:
        pass
    w = World()
    w.record = Record(SQLiteDocStore(tmp_path / "r.sqlite3"), LocalFileStore(tmp_path / "f"))
    w.model = FakeModel()
    queue = InlineQueue()
    w.app = create_app(w.record, model=w.model, settings=SETTINGS, queue=queue)
    queue.app = w.app
    w.client = TestClient(w.app)
    w.desk: AgentDesk = w.app.state.agent
    _, w.five = w.record.create_stay(name=NAME, uhid=UHID, sex="M", age="50", unit="Medicine|Unit 1", bed="5")
    _, w.twelve = w.record.create_stay(name=OTHER_NAME, uhid=OTHER_UHID, sex="F", age="40", unit="Medicine|Unit 1", bed="12")
    return w


class AppSession:
    def __init__(self, client):
        self.client = client

    def post(self, url, headers=None, timeout=None, json=None, data=None, files=None):
        return self.client.post(url, json=json, data=data, files=files)


class FakeHands:
    name = "ehospital"

    def __init__(self, answers=None):
        self.answers, self.seen, self.forgot, self.visits = list(answers or []), [], [], 0

    async def fetch_new(self, who, say):
        self.seen.append(dict(who))
        say("Signed in to eHospital")
        say("Opening lab reports")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def forget(self, who, keys):
        self.forgot += keys

    async def visit(self):
        self.visits += 1
        return 0

    async def ask(self, question, say=None):
        if say:
            say("Used saved skill: count vacant beds")
        self.last_skill = {"name": "bed_occupancy.count_vacant_beds", "state": "used"}
        return f"Answer: 3 free. Number 20269990001 for {NAME}", 0


def make_worker(world, answers=None, worker="laptop-1"):
    hands = FakeHands(answers)
    client = ServerClient("http://test", worker, session=AppSession(world.client), token=None)
    return Worker(client, hands, sleep=_no_wait, alive_s=3600), hands


async def _no_wait(_):
    await asyncio.sleep(0)


def command(world, text, admission_id=None):
    reply = world.client.post("/ask", json={"text": text, "admission_id": admission_id})
    assert reply.status_code == 200, reply.text
    return one(reply.json())


def one(body):
    """A jobs card with one job reads like the old single card: its task, its answer or its error."""
    card = body["card"]
    if card["kind"] != "jobs" or len(card["jobs"]) != 1:
        return body
    job = card["jobs"][0]
    inner = ({"kind": "agent", "task": job["task"], "text": ""} if job["task"] else job["answer"] or
             {"kind": "message", "text": job["error"] or ""})
    return {**body, "card": inner}


def lab(world, admission_id, *values, when=None):
    return world.client.post(f"/admissions/{admission_id}/report-values", json={"portal": "ehospital", "values": [
        {"test": t, "value": str(v), "number": float(v), "unit": "mmol/L", "reference": "3.5 - 5.1",
         "collected_at": when or datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None).isoformat(timespec="minutes"),
         "sample_no": f"S{t}{v}"} for t, v in values]}).json()


def test_spoken_numbers():
    assert "bed 5" in spoken_numbers("Get bed five's reports")
    assert "bed 12" in spoken_numbers("bed twelve")
    assert "bed 21" in spoken_numbers("bed twenty one")
    assert "bed 5" in spoken_numbers("bed paanch")
    assert spoken_numbers("do this") == "do this"


def test_cannot_is_answered_plainly_and_nothing_opens(world):
    card = command(world, "Book an MRI for bed 5")["card"]
    assert card["text"] == "I can't do that yet" and world.desk.store.query("agent_tasks") == []


def test_code_checks_the_bed(world):
    assert "no bed 99" in command(world, "get bed 99 reports")["card"]["text"]
    world.model.reply = {"jobs": [{"kind": "get_reports", "bed": "12"}]}
    card = command(world, "get the reports")["card"]
    assert card["text"].startswith("Which bed?") and world.desk.store.query("agent_tasks") == []


def test_the_patient_on_screen_and_a_spoken_name_work_without_a_bed(world):
    world.model.reply = {"jobs": [{"kind": "get_reports", "bed": None}]}
    assert command(world, "get the reports", world.twelve["id"])["intent"]["patient_id"] == world.twelve["id"]
    assert command(world, "get Zorawar's reports")["intent"]["patient_id"] == world.five["id"]


def test_the_patient_names_go_to_muse_so_a_name_finds_its_bed(world):
    command(world, "get Zorawar's reports")
    ward = world.model.prompts[-1].split("Beds and patients on this ward:", 1)[1].split("Open patient:", 1)[0]
    assert "Zorawar Testpatient" in ward and "Qamar Examplename" in ward
    assert "[patient]" not in world.model.prompts[-1]
    assert "zorawar's reports" in world.model.prompts[-1].lower()                 # the words as said, no name taken out


def test_watch_end_times():
    night = datetime(2026, 10, 30, 22, 0)
    assert watch_end(None, None, night) == datetime(2026, 10, 31, 8, 0)
    assert watch_end("06:30", None, night) == datetime(2026, 10, 31, 6, 30)
    assert watch_end("8 am", None, night) == datetime(2026, 10, 31, 8, 0)
    assert watch_end(None, 120, night) == datetime(2026, 10, 31, 0, 0)
    assert watch_end(None, 9000, night) == night + timedelta(hours=24)


def test_get_reports_life_cycle_with_values(world):
    task = command(world, GET)["card"]["task"]
    assert task["status"] == "waiting" and task["bed"] == "5"
    assert "laptop worker is off" in task["health"]
    worker, hands = make_worker(world, [Fetched("FOUND", found=9, new=2, values=[
        {"test": "SERUM K+", "value": "7.5", "number": 7.5, "unit": "mmol/L", "reference": "3.5 - 5.1", "collected_at": "2026-10-30T09:00"},
        {"test": "SERUM NA+", "value": "140", "number": 140, "unit": "mmol/L", "reference": "135 - 145", "collected_at": "2026-10-30T09:00"}])])
    assert asyncio.run(worker.run_once()) is True
    done = world.client.get(f"/agent/tasks/{task['id']}").json()
    assert done["status"] == "done" and done["new_reports"] == 2 and done["attention"] == 1
    assert done["summary"] == "2 new reports · 1 need attention"
    assert "Signed in to eHospital" in done["steps"] and "9 reports found, 2 new" in done["steps"]
    assert hands.seen == [{"uhid": UHID, "name": NAME}]
    labs = [f for f in world.record.facts(world.five["id"]) if f["type"] == "lab_result"]
    assert len(labs) == 2
    page = world.client.get(f"/admissions/{world.five['id']}/today").json()
    assert page["alerts"] and page["alerts"][0]["key"] == "potassium"


def test_second_run_is_no_new_reports(world):
    world.client.post(f"/admissions/{world.five['id']}/report-values", json={"portal": "ehospital", "values": []})
    task = command(world, GET)["card"]["task"]
    worker, _ = make_worker(world, [Fetched("NOTHING_NEW", found=9)])
    asyncio.run(worker.run_once())
    done = world.desk.get(task["id"])
    assert done["status"] == "done" and done["summary"] == "No new reports" and done["steps"][-2] == "9 reports found, 0 new"


def test_name_mismatch_stops_and_says_so(world):
    task = command(world, GET)["card"]["task"]
    worker, _ = make_worker(world, [Fetched("NAME_MISMATCH", found=3)])
    asyncio.run(worker.run_once())
    done = world.desk.get(task["id"])
    assert done["status"] == "failed" and done["summary"] == NAME_MISMATCH
    assert not [f for f in world.record.facts(world.five["id"]) if f["type"] == "lab_result"]


def test_the_runner_checks_every_row_before_ticking():
    step = {"name_column": "Patient Name"}
    row = lambda name: {"cells": {"Patient Name": name, "Sample No": "1"}}
    SkillRunner._check_patient(step, {"patient_name": "Mr. ZORAWAR TESTPATIENT"}, [row("zorawar testpatient")])
    SkillRunner._check_patient(step, {}, [row("anyone at all")])
    with pytest.raises(_Stop) as stop:
        SkillRunner._check_patient(step, {"patient_name": NAME}, [row(NAME), row(OTHER_NAME)])
    assert stop.value.status == "NAME_MISMATCH"
    with pytest.raises(_Stop):
        SkillRunner._check_patient(step, {"patient_name": NAME}, [{"cells": {"Sample No": "1"}}])


def test_the_real_skill_asks_for_the_name_check():
    skill = json.loads((Path(__file__).parents[2] / "skills/ehospital/lis.fetch_lab_reports_learned.json").read_text(encoding="utf-8"))
    step = next(s for s in skill["steps"] if s["action"] == "select_rows_paged")
    assert step["name_column"] == "Patient Name" and "patient_name" in skill["inputs"]


def test_ledger_forgets_a_report_that_never_reached_the_record(tmp_path):
    ledger = Ledger(tmp_path, "ehospital", UHID)
    ledger.remember(["a", "b"], [], "s")
    ledger.forget(["a"])
    assert ledger.seen_rows() == {"b"}
    assert UHID not in ledger.path.name


def test_a_post_that_fails_is_taken_again_next_time(world, tmp_path):
    task = command(world, GET)["card"]["task"]
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    worker, hands = make_worker(world, [Fetched("FOUND", found=1, new=1, pdfs=[str(tmp_path / "gone.pdf")], keys=["k1"])])
    asyncio.run(worker.run_once())
    assert hands.forgot == ["k1"] and world.desk.get(task["id"])["status"] == "working"


def test_portal_values_only_new_and_labelled(world):
    values = [{"test": "Potassium", "value": "4.0", "number": 4.0, "unit": "mmol/L", "reference": "3.5 - 5.1",
               "collected_at": "2026-10-30T09:00", "sample_no": "S1"}]
    new, skipped = add_portal_values(world.record, world.five["id"], values, "openmrs")
    assert len(new) == 1 and skipped == 0
    assert add_portal_values(world.record, world.five["id"], values, "openmrs") == ([], 1)
    assert world.record.source(new[0])["label"] == "OpenMRS report"
    page = world.client.get(f"/admissions/{world.five['id']}/today").json()
    assert page["sources"][new[0]]["label"] == "OpenMRS report"


def test_portal_pdf_is_labelled_and_sends_no_new_report_push(world):
    reply = world.client.post(f"/admissions/{world.five['id']}/reports", data={"portal": "ehospital"},
                              files={"file": ("lab.pdf", b"%PDF-1.4 not really", "application/pdf")}).json()
    source = world.record.source(reply["source_id"])
    assert source["label"] == "eHospital report" and source["author"] == "hospital-agent"


def test_stop_ends_a_waiting_task(world):
    task = command(world, GET)["card"]["task"]
    stopped = world.client.post(f"/agent/tasks/{task['id']}/stop").json()
    assert stopped["status"] == "stopped" and not stopped["active"]
    worker, hands = make_worker(world, [])
    asyncio.run(worker.run_once())
    assert hands.seen == []


def test_stop_while_working_ends_the_job(world):
    task = command(world, GET)["card"]["task"]
    worker, hands = make_worker(world, [Fetched("NOTHING_NEW", found=1)])
    real = hands.fetch_new

    async def stopped_midway(who, say):
        world.client.post(f"/agent/tasks/{task['id']}/stop")
        return await real(who, lambda line: None)
    hands.fetch_new = stopped_midway
    asyncio.run(worker.run_once())
    assert world.desk.get(task["id"])["status"] == "stopped"


def test_another_worker_cannot_touch_a_claimed_task(world):
    task = command(world, GET)["card"]["task"]
    one = ServerClient("http://test", "one", session=AppSession(world.client), token=None)
    two = ServerClient("http://test", "two", session=AppSession(world.client), token=None)
    assert one.next()["task"]["id"] == task["id"]
    assert two.next()["refused"]
    with pytest.raises(TaskGone):
        two.step(task["id"], "mine now")
    with pytest.raises(TaskGone):
        two.patient(task["id"])


def test_a_task_keeps_the_words_the_robot_wrote(world):
    task = command(world, f"get Zorawar's reports {UHID}")["card"]["task"]
    client = ServerClient("http://test", "one", session=AppSession(world.client), token=None)
    client.next()
    client.step(task["id"], f"Opening {NAME} number {UHID}", thought=f"{NAME}")
    client.finish(task["id"], "done", f"Found {NAME} {UHID}", answer=f"{NAME} {UHID}")
    words = json.dumps(world.desk.store.query("agent_tasks"))
    assert NAME in words and UHID in words                                           # shown as written, not hidden


def test_run_log_result_holds_no_identity():
    result = {"outputs": {"uhid": UHID, "patient_name": NAME, "selected_count": 1,
                          "rows": [{"key": "k", "cells": {"Patient Name": NAME, "Registration No": UHID, "test Name": "KFT"}}]}}
    words = json.dumps(result_without_identity(result))
    assert UHID not in words and NAME not in words and "KFT" in words


def test_worker_routes_need_a_machine_or_the_owner(tmp_path):
    from wardos.chain.settings import AuthSettings
    record = Record(SQLiteDocStore(tmp_path / "s.sqlite3"), LocalFileStore(tmp_path / "ff"))
    app = create_app(record, model=FakeModel(), settings=SETTINGS, queue=InlineQueue(), auth_settings=AuthSettings(
        client_id="c", client_secret="s", cookie_secret="x" * 40, public_url="https://w.test", owner_emails=("o@x.test",),
        machine_accounts=("robot@x.test",)), verify_token=lambda token, audience: {"email": token, "email_verified": True})
    anyone = TestClient(app)
    for path in ("/agent/worker/next", "/agent/worker/tasks/t1/patient", "/agent/worker/give-up"):
        assert anyone.post(path, json={"worker": "x", "line": "x"}).status_code == 401
    assert anyone.post("/agent/worker/next", json={"worker": "x"}, headers={"Authorization": "Bearer stranger@x.test"}).status_code == 403
    assert anyone.post("/agent/worker/next", json={"worker": "x"}, headers={"Authorization": "Bearer robot@x.test"}).status_code == 200
    for method, path in (("get", "/agent/watches"), ("get", "/agent/tasks/t1"), ("post", "/agent/command"), ("get", "/agent/worker")):
        assert getattr(anyone, method)(path).status_code == 401
    assert anyone.get("/agent/watches", headers={"Authorization": "Bearer robot@x.test"}).status_code == 401


def start_watch(world, text="Watch bed twelve's potassium every 15 minutes tonight"):
    card = command(world, text)["card"]
    assert card["kind"] == "agent", card
    return card["task"]


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def go(self, **kwargs):
        self.at += timedelta(**kwargs)


@pytest.fixture
def clock(world):
    now = Clock()
    world.desk.now = now
    return now


def test_watch_says_what_it_will_do_and_is_listed(world):
    task = start_watch(world)
    assert task["title"].startswith("Watching bed 12 · potassium · every 15 min until 8 AM")
    rows = world.client.get("/agent/watches").json()["watches"]
    assert [r["id"] for r in rows] == [task["id"]]
    listed = command(world, "what are you watching")["card"]
    assert listed["kind"] == "agent_watches" and listed["watches"][0]["id"] == task["id"]


def test_same_watch_twice_changes_the_first(world):
    one = start_watch(world)
    two = start_watch(world)
    assert one["id"] == two["id"] and len(world.client.get("/agent/watches").json()["watches"]) == 1


def test_schedule_a_check_runs_only_when_due(world, clock):
    task = start_watch(world)
    worker, hands = make_worker(world, [Fetched("NOTHING_NEW", found=3), Fetched("NOTHING_NEW", found=3)])
    assert asyncio.run(worker.run_once()) is True
    row = world.desk.get(task["id"])
    assert row["checks"] == 1 and row["steps"][-1].endswith("· checked · no new potassium")
    assert asyncio.run(worker.run_once()) is False
    clock.go(minutes=14)
    assert asyncio.run(worker.run_once()) is False
    clock.go(minutes=2)
    assert asyncio.run(worker.run_once()) is True and world.desk.get(task["id"])["checks"] == 2


def test_normal_value_is_a_quiet_line_and_no_alert(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("FOUND", found=1, new=1, values=[
        {"test": "SERUM K+", "value": "4.2", "number": 4.2, "unit": "mmol/L", "reference": "3.5 - 5.1", "collected_at": "2026-10-30T21:00"}])])
    asyncio.run(worker.run_once())
    row = world.desk.get(task["id"])
    assert "new SERUM K+ 4.2 · not dangerous" in row["steps"][-1] and not row.get("alert")


def test_danger_line_alert_trend_and_related_medicine(world, clock):
    lab(world, world.twelve["id"], ("SERUM K+", 5.1), when="2026-10-29T08:00")
    note = world.record.add_source(world.twelve["id"], "text", text="KCl drip", mime="text/plain", author="d")[0]
    page = world.record.save_pages(note["id"], [{"number": 1, "text": "KCl drip"}])[0]
    world.record.apply(note["id"], [
        m.fact_doc(admission_id=world.twelve["id"], source_id=note["id"], page_id=page["id"], index=0, type="med_event", key="kcl",
                   label="KCl", value={"text": "KCl drip", "event": "started"}, clinical_time="2026-10-29", time_precision="day",
                   assertion="present", quote="KCl drip", quote_found=True)])
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("FOUND", found=2, new=1, values=[
        {"test": "SERUM K+", "value": "7.5", "number": 7.5, "unit": "mmol/L", "reference": "3.5 - 5.1", "collected_at": "2026-10-30T21:00"}])])
    asyncio.run(worker.run_once())
    row = world.desk.get(task["id"])
    assert " · DANGER · SERUM K+ 7.5 · normal 3.5 - 5.1 · collected 30/10 21:00 · earlier: 5.1 (29/10 08:00)" in row["steps"][-1]
    alert = row["alert"]
    assert alert["earlier"] == [{"value": "5.1", "time": "29/10 08:00"}] and alert["medicines"] == ["kcl"]
    assert alert["report_url"].endswith("/file") and alert["value"] == "7.5"
    log = world.client.get(f"/agent/watches/{task['id']}/log").json()
    assert log["lines"][-1]["danger"] and log["alert"]["value"] == "7.5"
    page = world.client.get(f"/agent/watches/{task['id']}/night-log")
    assert page.status_code == 200 and "DANGER" in page.text


def test_one_push_for_one_dangerous_report_and_none_for_normal(world, clock):
    alerts = world.app.state.alerts
    sent = []
    alerts.settings = type("S", (), {"configured": True, "public": "p"})()
    alerts._devices = lambda email=None: [{"id": "d", "subscription": {}}]
    alerts._send = lambda devices, payload: (sent.append(payload), {"sent": 1, "failed": 0})[1]
    now = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
    earlier, newer = (now - timedelta(hours=2)).isoformat(timespec="minutes"), (now - timedelta(hours=1)).isoformat(timespec="minutes")
    lab(world, world.twelve["id"], ("SERUM K+", 4.2), when=earlier)
    assert sent == []
    lab(world, world.twelve["id"], ("SERUM K+", 7.5), when=newer)
    assert len(sent) == 1 and sent[0]["title"] == "WardOS: Bed 12 - dangerous value. Tap to open." and not sent[0]["silent"]
    lab(world, world.twelve["id"], ("SERUM K+", 7.5), when=newer)
    assert len(sent) == 1
    assert NAME not in json.dumps(sent) and OTHER_NAME not in json.dumps(sent)


def test_an_old_dangerous_value_from_a_fetch_stays_on_screen_but_wakes_nobody():
    now = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)
    after = {"alerts": [{"source_id": "agent1", "time": "2026-10-20T08:00"}, {"source_id": "agent2", "time": "2026-10-31T01:00"},
                        {"source_id": "photo", "time": "2026-10-20T08:00"}], "notices": [{"source_id": "agent1", "kind": "info"}]}
    kept = quiet_for_agent(after, {"agent1": {}, "agent2": {}}, now=now)
    assert [a["source_id"] for a in kept["alerts"]] == ["agent2", "photo"] and kept["notices"] == []


def test_older_values_are_one_quiet_line_never_danger(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("FOUND", found=2, new=2, values=[
        {"test": "SERUM K+", "value": "7.5", "number": 7.5, "reference": "3.5 - 5.1", "collected_at": "2026-10-20T09:00"},
        {"test": "SERUM K+", "value": "7.1", "number": 7.1, "reference": "3.5 - 5.1", "collected_at": "2026-10-21T09:00"}])])
    asyncio.run(worker.run_once())
    row = world.desk.get(task["id"])
    assert "2 older potassium values added, not new tonight" in row["steps"][-1] and not row.get("alert")


def test_stop_a_watch_by_voice_and_by_button(world, clock):
    task = start_watch(world)
    assert "Stopped watching bed 12 potassium" in command(world, "stop watching bed twelve")["card"]["text"]
    assert world.desk.get(task["id"])["status"] == "stopped" and world.client.get("/agent/watches").json()["watches"] == []
    again = start_watch(world)
    assert world.client.post(f"/agent/tasks/{again['id']}/stop").json()["status"] == "stopped"
    worker, hands = make_worker(world, [])
    assert asyncio.run(worker.run_once()) is False and hands.seen == []


def test_worker_missing_is_said_plainly(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("NOTHING_NEW", found=1)])
    asyncio.run(worker.run_once())
    assert world.client.get(f"/agent/tasks/{task['id']}").json()["health"].startswith("Last check 21:30")
    clock.go(minutes=45)
    view = world.client.get(f"/agent/tasks/{task['id']}").json()
    assert view["health"] == "Last check 21:30 · The laptop worker is not answering" and view["active"]


def test_ehospital_down_is_said_plainly_and_the_next_round_tries(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [RuntimeError("no page"), Fetched("NOTHING_NEW", found=1)])

    async def down_then_up(who, say, _hands=worker.hands, real=worker.hands.fetch_new):
        try:
            return await real(who, say)
        except RuntimeError:
            return Fetched("PROBLEM", problem="eHospital not answering (RuntimeError)")
    worker.hands.fetch_new = down_then_up
    asyncio.run(worker.run_once())
    view = world.client.get(f"/agent/tasks/{task['id']}").json()
    assert view["health"] == "Last check 21:30 · eHospital not answering" and view["active"]
    assert "trying again next round" in view["steps"][-1] and view["problems"] == 1
    clock.go(minutes=16)
    asyncio.run(worker.run_once())
    assert world.client.get(f"/agent/tasks/{task['id']}").json()["health"] == "Last check 21:46"


def test_missed_checks_are_written_in_the_night_log(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("NOTHING_NEW", found=1), Fetched("NOTHING_NEW", found=1)])
    asyncio.run(worker.run_once())
    clock.go(minutes=44)
    asyncio.run(worker.run_once())
    steps = world.desk.get(task["id"])["steps"]
    assert "no checks since 21:30 · the laptop or the internet was off" in steps[-2] and "no new potassium" in steps[-1]


def test_name_mismatch_ends_the_watch(world, clock):
    task = start_watch(world)
    worker, _ = make_worker(world, [Fetched("NAME_MISMATCH", found=1)])
    asyncio.run(worker.run_once())
    row = world.desk.get(task["id"])
    assert row["status"] == "failed" and NAME_MISMATCH in row["steps"][-1]


def test_a_watch_ends_at_its_time_even_with_the_worker_off(world, clock):
    task = start_watch(world, "Watch bed 12 potassium every 15 minutes until 22:00")
    assert world.desk.get(task["id"])["status"] == "waiting"
    clock.at = datetime.now(timezone.utc) + timedelta(hours=25)
    rows = world.client.get("/agent/watches").json()["watches"]
    assert rows == [] and world.desk.get(task["id"])["status"] == "done"
    assert "end time reached" in world.desk.get(task["id"])["steps"][-1]


def test_stale_tasks_say_what_happened(world, clock):
    waiting = command(world, GET)["card"]["task"]
    clock.go(minutes=31)
    assert "laptop worker was off" in world.client.get(f"/agent/tasks/{waiting['id']}").json()["summary"]
    working = command(world, "get bed 12 reports")["card"]["task"]
    client = ServerClient("http://test", "w", session=AppSession(world.client), token=None)
    assert client.next()["task"]["id"] == working["id"]
    clock.go(minutes=13)
    assert "laptop stopped" in world.client.get(f"/agent/tasks/{working['id']}").json()["summary"]


def test_login_guard_stops_everything_then_one_new_try(world, clock):
    watch = start_watch(world)
    worker, _ = make_worker(world, [Fetched("PROBLEM", problem="eHospital login failed", logins=2, login_failed=True),
                                    Fetched("PROBLEM", problem="eHospital login failed", logins=2, login_failed=True),
                                    Fetched("NOTHING_NEW", found=1)])
    asyncio.run(worker.run_once())
    assert worker.failed_logins == 2 and world.desk.get(watch["id"])["status"] == "working"
    clock.go(minutes=16)
    asyncio.run(worker.run_once())
    assert worker.failed_logins >= FAILED_LOGINS_LIMIT and worker.stopped
    row = world.desk.get(watch["id"])
    assert row["status"] == "failed" and LOGIN_STOPPED in row["steps"][-1]
    assert asyncio.run(worker.run_once()) is False
    task = command(world, GET)["card"]["task"]
    asyncio.run(worker.run_once())
    assert world.desk.get(task["id"])["status"] == "done" and not worker.stopped and worker.failed_logins == 0


def test_keep_alive_only_while_a_watch_is_waiting_far_ahead(world, clock):
    ticks = {"t": 0.0}
    worker, hands = make_worker(world, [Fetched("NOTHING_NEW", found=1)])
    worker.clock = lambda: ticks["t"]
    worker.last_visit = 0.0
    ticks["t"] = 3600
    asyncio.run(worker.run_once())
    assert hands.visits == 0
    world.model.reply = {"jobs": [{"kind": "watch", "bed": "12", "test": "potassium", "every_minutes": 60}]}
    command(world, "Watch bed 12 potassium every 60 minutes tonight")
    asyncio.run(worker.run_once())
    ticks["t"] += 13 * 60
    clock.go(minutes=13)
    asyncio.run(worker.run_once())
    assert hands.visits == 1


def test_cloud_robot_resident_runs_the_task_then_leaves_when_idle(world):
    task = command(world, GET)["card"]["task"]
    worker, hands = make_worker(world, [Fetched("NOTHING_NEW", found=4)], worker="cloud-1")
    left = asyncio.run(worker.run_resident(idle_exit_s=0, max_run_s=60))
    assert left == "nothing to do" and world.desk.get(task["id"])["status"] == "done" and len(hands.seen) == 1


def test_cloud_robot_with_nothing_waiting_leaves_at_once_without_opening_the_hospital(world):
    worker, hands = make_worker(world, [], worker="cloud-1")
    assert asyncio.run(worker.run_resident(idle_exit_s=600, max_run_s=60)) == "nothing to do"
    assert hands.seen == [] and hands.visits == 0


def test_a_second_robot_is_refused_while_one_is_working(world):
    world.desk.heartbeat("cloud-first")
    worker, _ = make_worker(world, [], worker="cloud-second")
    assert asyncio.run(worker.run_resident(idle_exit_s=0, max_run_s=60)) == "another worker is running"


def test_login_counts_are_kept_on_the_site_so_a_restarted_robot_cannot_forget_them(world):
    first, _ = make_worker(world, [Fetched("PROBLEM", problem="eHospital login failed", logins=1, login_failed=True)], worker="cloud-1")
    command(world, GET)
    asyncio.run(first.run_once())
    saved = world.desk.logins("x")
    assert saved["failed"] == 1 and len(saved["times"]) == 1
    fresh, _ = make_worker(world, [], worker="cloud-2")
    fresh._load_guard()
    assert fresh.failed_logins == 1 and len(fresh.login_times) == 1
    world.desk.logins("x", add=2, failed=True)
    assert world.desk.logins("x")["failed"] == 3


def test_cloud_robot_wording_replaces_the_laptop_wording(world):
    world.desk.cloud_robot = True
    task = command(world, GET)["card"]["task"]
    assert "hospital robot is starting" in task["health"] and "laptop" not in task["health"]


def test_hospital_question_task_says_the_skill_and_lists_for_the_tasks_screen(world):
    question = command(world, "how many vacant beds in eHospital")["card"]["task"]
    worker, _ = make_worker(world, [], worker="one")
    asyncio.run(worker.run_once())
    task = world.desk.get(question["id"])
    assert "Used saved skill: count vacant beds" in task["steps"]
    assert task["skill"] == "bed_occupancy.count_vacant_beds" and task["skill_state"] == "used"
    rows = world.client.get("/agent/tasks").json()["tasks"]
    row = next(r for r in rows if r["id"] == question["id"])
    assert row["job"] == "Hospital question" and row["patient_label"] == "Ward" and row["status"] == "done"
    assert "3 free" in row["answer"] and row["time"] and row["skill_state"] == "used"


def test_skill_step_texts():
    from types import SimpleNamespace as N
    from wardos.hospital.skill_notes import end_lines, skill_title, start_line
    assert skill_title("bed_occupancy.count_vacant_beds") == "count vacant beds"
    assert skill_title("beds.vacant.count") == "beds vacant count"
    assert start_line("run", "admissions.count_admitted_patients") == "Used saved skill: count admitted patients"
    assert start_line("explore", None) == "Learning a new skill…"
    assert end_lines(N(skill="a.count_x", learned=True, repaired=False, decision="explore")) == (["Saved new skill: count x"], "learned")
    assert end_lines(N(skill="a.count_x", learned=False, repaired=True, decision="repair")) == (["Repaired skill: count x"], "repaired")
    assert end_lines(N(skill="a.count_x", learned=False, repaired=False, decision="run")) == ([], "used")




# Muse reads every command and returns a list of jobs, or one question with options ----
def jobs_of(world, text, **extra):
    reply = world.client.post("/ask", json={"text": text, **extra})
    assert reply.status_code == 200, reply.text
    return reply.json()["card"]


def test_three_jobs_from_one_sentence(world):
    world.model.reply = {"jobs": [
        {"kind": "record_question", "bed": "5", "text": "medicines and diagnosis of bed 5"},
        {"kind": "record_question", "bed": "12", "text": "medicines and diagnosis of bed 12"},
        {"kind": "get_reports", "bed": "12", "test": "potassium", "text": "fetch potassium of bed 12"}]}
    card = jobs_of(world, "medicines and diagnosis of web 5 and also bed 12, fetch potassium of bed 12")
    assert card["kind"] == "jobs" and card["title"] == "3 things" and [j["id"] for j in card["jobs"]] == ["j1", "j2", "j3"]
    assert card["jobs"][0]["answer"] and card["jobs"][1]["answer"] and card["jobs"][2]["task"]["bed"] == "12"
    assert len(world.model.prompts) == 1 and len(world.desk.store.query("agent_tasks")) == 1


def test_a_question_with_options_and_the_reply_continues_it(world):
    world.model.replies = [{"ask": {"text": "Which test?", "options": [{"label": "Potassium", "add": "potassium"}]}},
                           {"jobs": [{"kind": "watch", "bed": "12", "test": "potassium", "text": "watch potassium of bed 12"}]}]
    card = jobs_of(world, "watch bed 12")
    assert card["kind"] == "ask_ai" and card["options"][0]["add"] == "potassium" and card["allow_other"] and card["context_id"]
    assert world.desk.store.query("agent_tasks") == []
    done = jobs_of(world, "potassium", reply_to=card["context_id"])
    assert done["kind"] == "jobs" and done["jobs"][0]["task"]["test"] == "potassium"
    assert "watch bed 12" in world.model.prompts[1] and "Which test?" in world.model.prompts[1]
    assert jobs_of(world, "again", reply_to=card["context_id"])["kind"] == "jobs"        # the context is used once


def test_after_two_questions_it_says_it_cannot(world):
    ask = {"ask": {"text": "Which bed?", "options": []}}
    world.model.replies = [ask, ask, ask]
    first = jobs_of(world, "do the thing")
    second = jobs_of(world, "hmm", reply_to=first["context_id"])
    third = jobs_of(world, "hmm", reply_to=second["context_id"])
    assert second["kind"] == "ask_ai" and third["kind"] == "jobs" and third["jobs"][0]["error"]


def test_when_muse_does_not_answer_nothing_starts(world):
    world.model.fail = True
    for words in (GET, "potassium of bed 5"):
        reply = world.client.post("/ask", json={"text": words})
        assert reply.status_code == 502 and "nothing was started" in reply.json()["detail"]
    assert world.desk.store.query("agent_tasks") == []


def test_a_duplicate_of_a_running_job_is_skipped(world):
    first = jobs_of(world, GET)
    assert first["jobs"][0]["task"]
    again = jobs_of(world, GET)
    assert again["jobs"] == [] and "already running" in again["skipped"][0]
    assert len(world.desk.store.query("agent_tasks")) == 1


def test_an_order_is_cannot_and_nothing_opens(world):
    card = jobs_of(world, "Book an MRI for bed 5")
    assert card["jobs"][0]["error"] == "I can't do that yet" and world.desk.store.query("agent_tasks") == []


def test_resolve_keeps_the_shortest_gap():
    ward = [{"id": "a", "bed": "12", "name_words": []}]
    reply = {"action": "watch", "bed": "12", "test": "K", "every_minutes": 1}
    assert resolve(reply, "watch bed 12 K every 1 minute", ward, None, min_every=5).every_min == 5
    done = resolve(reply, "watch bed 12 K every 1 minute", ward, None, min_every=1)
    assert done.every_min == 1 and done.test == "potassium"
