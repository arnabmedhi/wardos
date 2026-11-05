"""Offline checks for the demo robot."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

import openmrs_seed as seed
from openmrs_watch import values_from_rows
from demo_robot_context import build_context, FILES
from wardos.chain.docstore import SQLiteDocStore
from wardos.chain import model as m
from wardos.hospital.demo_record import GuestStore, guest_record, ingest, snapshot
from wardos.hospital.demo_robot import Robot, choose_demo, machine_alert
from wardos.hospital.demo_site import Maintainer, TAG, tagged
from wardos.hospital.demo_tasks import TaskQueue, start_demo_task


@pytest.fixture
def store(tmp_path):
    return SQLiteDocStore(tmp_path / "demo.db")


def make_guest(store, guest="guest-a", bed="12"):
    record = guest_record(store, guest)
    patient = record.create_patient(name="Made-up demo patient", sex="M")
    admission = record.create_admission(patient["id"], unit="Medicine Unit 1", bed=bed)
    record.store.put(m.ADMISSIONS, dict(admission, demo_bed=bed))
    return record, admission["id"]


def task(store, admission, ident="task-a", guest="guest-a", kind="get_reports"):
    row = dict(id=ident, guest=guest, admission_id=admission, bed="12", kind=kind, test="potassium",
               status="waiting", steps=[], created_at=datetime.now(timezone.utc).isoformat())
    store.put("tasks", row)
    return row


def values(number="4.2", when=None):
    now = datetime.now().replace(second=0, microsecond=0)
    return values_from_rows([{"cells": {"Test name": "Serum potassium", "Result date": (when or now).strftime("%d-%b-%Y, %I:%M %p"),
                                       "Value": number + " mmol/L", "Reference range": "3.5 – 5.6 mmol/L"}}], now)[0]


def test_guest_cannot_read_write_or_query_other_guest(store):
    a, admission = make_guest(store)
    b = GuestStore(store, "guest-b")
    assert b.get(m.ADMISSIONS, admission) is None
    assert not b.query(m.ADMISSIONS)
    with pytest.raises(ValueError):
        b.put(m.ADMISSIONS, {"id": admission})
    with pytest.raises(ValueError):
        GuestStore(SimpleNamespace(prefix="wardos_"), "guest-a")


def test_only_new_and_real_record_merge_and_danger_outbox(store):
    record, admission = make_guest(store)
    work = task(store, admission)
    assert len(ingest(record, work, values())) == 1
    assert ingest(record, work, values()) == []
    later = datetime.now() + timedelta(minutes=1)
    assert len(ingest(record, work, values("6.8", later))) == 1
    assert len(record.facts(admission)) == 2
    assert snapshot(record, admission)["alerts"]
    events = record.store.query("notification_outbox")
    assert any(e["event"]["kind"] == "danger" for e in events)
    assert all(s["kind"] == "portal_report" and s["label"] == "OpenMRS report" for s in record.store.query(m.SOURCES))
    assert record.read_file(record.store.query(m.SOURCES)[0])


def test_guest_isolation_and_wrong_bed(store):
    a, first = make_guest(store)
    b, second = make_guest(store, "guest-b")
    ingest(a, task(store, first), values())
    assert not b.facts(second)
    with pytest.raises(ValueError):
        ingest(a, dict(task(store, first), bed="11"), values())


def test_task_is_claimed_once_and_terminal_steps_cannot_be_changed(store):
    _, admission = make_guest(store)
    task(store, admission)
    queue = TaskQueue(store)
    current = queue.claim("task-a")
    assert queue.claim("task-a") is None
    queue.step(current, "Done", terminal="done")
    with pytest.raises(RuntimeError):
        queue.step(current, "Something else")


@pytest.mark.parametrize("field,value", [("guest", "../real"), ("bed", "1"), ("kind", "ehospital")])
def test_invalid_task_refused(store, field, value):
    _, admission = make_guest(store)
    row = task(store, admission)
    store.put("tasks", {**row, field: value})
    with pytest.raises(ValueError):
        TaskQueue(store).claim(row["id"])


def test_stale_task_marked_failed_and_lock_has_owner(store):
    _, admission = make_guest(store)
    task(store, admission)
    now = datetime.now(timezone.utc)
    queue = TaskQueue(store, now=lambda: now)
    queue.claim("task-a")
    queue.now = lambda: now + timedelta(minutes=19)
    queue.expire_stalled()
    assert store.get("tasks", "task-a")["status"] == "failed"
    assert queue.acquire("maintenance", "a")
    assert not queue.acquire("maintenance", "b")
    queue.release("maintenance", "b")
    assert not queue.acquire("maintenance", "b")
    queue.release("maintenance", "a")
    assert queue.acquire("maintenance", "b")


def test_start_helper_sends_only_task_id():
    sent = []
    session = SimpleNamespace(post=lambda *args, **kw: sent.append((args, kw)) or
                              SimpleNamespace(status_code=200, json=lambda: {"name": "operation"}))
    assert start_demo_task("task-123", session=session) == "operation"
    assert sent[0][1]["json"]["overrides"]["containerOverrides"][0]["env"] == [
        {"name": "WARDOS_DEMO_TASK_ID", "value": "task-123"}]
    with pytest.raises(ValueError):
        start_demo_task("../other", session=session)


@pytest.mark.asyncio
async def test_meta_choice_reuses_client_and_bed_cannot_change():
    class Models:
        async def generate(self, prompt, **kw):
            assert "patient_uuid" not in prompt and "guest-a" not in prompt
            return {"bed": "12", "skill": "lab.results.latest"}, None
    library = SimpleNamespace(list=lambda site: [{"kind": "read", "skill": "lab.results.latest"}])
    assert await choose_demo(Models(), {"bed": "12", "kind": "get_reports", "test": None}, library) == "lab.results.latest"
    with pytest.raises(ValueError):
        await choose_demo(Models(), {"bed": "11", "kind": "get_reports"}, library)


def test_machine_callback_uses_existing_endpoint():
    calls = []
    machine_alert("Put back", url="https://example.test", send=lambda *a: calls.append(a) or SimpleNamespace(status_code=200))
    assert calls == [("https://example.test/internal/demo-alert", {"line": "Put back"})]


class Clock:
    def __init__(self):
        self.time = datetime.now().replace(second=0, microsecond=0)
    def now(self):
        return self.time
    async def sleep(self, seconds):
        self.time += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class Maintenance:
    site = None
    people = {}
    def __init__(self):
        self.checks = self.injected = 0
        self.removed = []
        self.lines = []
        self.clock = Clock()
    def check(self):
        self.checks += 1
    def add_potassium(self, task):
        self.injected += 1
    def cleanup(self, *, task_id=None):
        self.removed.append(task_id)
    def alert(self, line):
        self.lines.append(line)


def reader_factory(maintenance, fail=False):
    class Reader:
        usage = {"usd": 0}
        def __init__(self, task, site, people, step):
            self.step = step
        async def __aenter__(self):
            self.step("Signed in")
            return self
        async def __aexit__(self, *args):
            pass
        async def read(self):
            if fail:
                raise RuntimeError("private error must not be shown")
            return values("6.8" if maintenance.injected else "4.2", maintenance.clock.now()) if maintenance.injected else values()
    return Reader


@pytest.mark.asyncio
async def test_get_then_no_new_reports(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path)
    task(store, admission)
    assert await robot.run("task-a")
    task(store, admission, ident="task-b")
    assert await robot.run("task-b")
    assert "No new reports" in store.get("tasks", "task-b")["steps"]


@pytest.mark.asyncio
async def test_one_new_report_is_said_in_the_singular(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    base = reader_factory(maintenance)

    class OneValue(base):
        async def read(self):
            return (await super().read())[:1]
    robot = Robot(TaskQueue(store), maintenance, reader=OneValue, private=tmp_path)
    task(store, admission)
    assert await robot.run("task-a")
    lines = [line for line in store.get("tasks", "task-a")["steps"] if "new report" in line]
    assert len(lines) == 1 and lines[0].endswith(": 1 new report")


@pytest.mark.asyncio
async def test_watch_injects_after_two_quiet_rounds_and_only_cleans_own_report(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path,
                  now=maintenance.clock.now, sleep=maintenance.clock.sleep, minutes=3)
    task(store, admission, kind="watch")
    assert await robot.run("task-a")
    work = store.get("tasks", "task-a")
    assert work["status"] == "done" and maintenance.injected == 1 and maintenance.removed == ["task-a"]
    assert any("DANGER" in line for line in work["steps"])
    assert any(e["event"]["kind"] == "danger" for e in guest_record(store, "guest-a").store.query("notification_outbox"))


@pytest.mark.asyncio
async def test_three_read_failures_stop(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance, fail=True), private=tmp_path,
                  sleep=maintenance.clock.sleep)
    task(store, admission)
    assert not await robot.run("task-a")
    assert "three failed report reads" in store.get("tasks", "task-a")["steps"][-1]
    assert "private error" not in json.dumps(store.get("tasks", "task-a"))


@pytest.mark.asyncio
async def test_concurrent_guest_tasks_and_duplicate_delivery(store, tmp_path):
    _, first = make_guest(store)
    _, second = make_guest(store, "guest-b")
    task(store, first)
    task(store, second, ident="task-b", guest="guest-b")
    maintenance = Maintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path)
    assert await asyncio.gather(robot.run("task-a"), robot.run("task-b"), robot.run("task-a")) == [True, True, False]
    assert len(guest_record(store, "guest-a").facts(first)) == len(guest_record(store, "guest-b").facts(second)) == 1


class FakeSite:
    def __init__(self):
        self.present = True
        self.changed = False
        self.deleted = []
        self.rows = [{"uuid": "report", "encounterDatetime": seed.iso(datetime(2026, 10, 30, 1, tzinfo=timezone.utc)),
                      "obs": [{"concept": {"uuid": seed.POTASSIUM}, "value": 4.2}]}]
    def beds(self):
        return {"WOS12": {"status": "OCCUPIED"}}
    def find_patient(self, person):
        return {"uuid": "patient", "display": "FAKE - Demo Wardos"} if self.present else None
    def active_visit(self, uuid):
        return {"uuid": "visit", "startDatetime": seed.iso(datetime(2026, 10, 30, tzinfo=timezone.utc))}
    def lab_encounters(self, uuid):
        return deepcopy(self.rows)
    def delete(self, path):
        self.deleted.append(path)
        self.rows = [r for r in self.rows if path != "/encounter/" + r["uuid"]]
    def add_labs(self, patient, visit, when, results):
        self.rows.append({"uuid": "fixed", "encounterDatetime": seed.iso(when),
                          "obs": [{"concept": {"uuid": r["concept"]}, "value": r["value"]} for r in results]})


@pytest.mark.parametrize("broken", ["gone", "changed"])
def test_maintenance_restores_missing_patient_or_changed_value(store, monkeypatch, broken):
    site = FakeSite()
    people = {"12": {"reports": [{"offset_s": 0, "results": [{"concept": seed.POTASSIUM, "value": 4.2}]}]}}
    site.present = broken != "gone"
    if broken == "changed":
        site.rows[0]["obs"][0]["value"] = 9.9
    monkeypatch.setattr(seed, "check", lambda *args: 0)
    monkeypatch.setattr(seed, "seed", lambda *args: setattr(site, "present", True))
    lines = []
    maintainer = Maintainer(site, people, TaskQueue(store), alert=lines.append)
    assert maintainer.check()["result"] == "Put back"
    assert maintainer.inspect()[1] == [] and len(lines) == 1
    assert store.get("status", "openmrs")["beds"][0]["bed"] == "12"


def test_cleanup_only_own_or_old_tagged_reports(store):
    site = FakeSite()
    now = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)
    for ident, age, comment in [("own", 0, TAG + "task-a"), ("other", 0, TAG + "task-b"),
                                ("old", 16, TAG + "task-c"), ("untagged", 30, "")]:
        site.rows.append({"uuid": ident, "encounterDatetime": seed.iso(now - timedelta(minutes=age)),
                          "obs": [{"comment": comment}]})
    maintenance = Maintainer(site, {"12": {}}, TaskQueue(store), now=lambda: now)
    assert maintenance.cleanup(task_id="task-a") == 1
    assert site.deleted == ["/encounter/own"]
    assert maintenance.cleanup() == 1
    assert site.deleted == ["/encounter/own", "/encounter/old"]


def test_notification_failure_does_not_erase_successful_repair(store):
    def fail(line):
        raise RuntimeError("callback unavailable")
    maintenance = Maintainer(FakeSite(), {}, TaskQueue(store), alert=fail)
    maintenance.notify("Put back")
    assert store.get("status", "owner_alert")["result"] == "Failed"


def test_maintenance_finds_and_repairs_renamed_demo_patient_by_saved_uuid(store, monkeypatch):
    class IdentitySite(FakeSite):
        def __init__(self):
            super().__init__()
            self.given, self.gender = "Ravi", "M"
        def find_patient(self, person):
            return super().find_patient(person) if self.given == person["given"] else None
        def get(self, path, **kw):
            return {"uuid": "patient", "voided": False,
                    "person": {"uuid": "person", "gender": self.gender, "birthdate": "1959-07-22",
                               "names": [{"uuid": "name", "givenName": self.given, "familyName": "Wardos", "preferred": True}]}}
        def post(self, path, body):
            if "/name/" in path:
                self.given = body["givenName"]
            else:
                self.gender = body["gender"]
    site = IdentitySite()
    person = {"given": "Ravi", "family": "Wardos", "gender": "M", "birthdate": "1959-07-22",
              "reports": [{"offset_s": 0, "results": [{"concept": seed.POTASSIUM, "value": 4.2}]}]}
    monkeypatch.setattr(seed, "check", lambda *args: 0)
    monkeypatch.setattr(seed, "seed", lambda *args: None)
    maintenance = Maintainer(site, {"12": person}, TaskQueue(store))
    assert maintenance.check()["result"] == "All there"
    site.given, site.gender = "Changed", "F"
    assert maintenance.check()["result"] == "Put back"
    assert site.given == "Ravi" and site.gender == "M"


@pytest.mark.asyncio
async def test_maintenance_busy_wait_does_not_count_as_three_failures(store, tmp_path):
    from wardos.hospital.demo_site import MaintenanceBusy
    _, admission = make_guest(store)
    maintenance = Maintenance()
    def busy_then_work():
        maintenance.checks += 1
        if maintenance.checks < 5:
            raise MaintenanceBusy()
    maintenance.check = busy_then_work
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path,
                  sleep=maintenance.clock.sleep)
    task(store, admission)
    assert await robot.run("task-a")
    assert maintenance.checks == 5


def test_watch_limit_cannot_be_raised(store):
    with pytest.raises(ValueError):
        Robot(TaskQueue(store), Maintenance(), minutes=11)


def test_seed_secret_compaction_is_lossless_and_checks_size():
    from demo_robot_secrets import seed_payload
    people = {"12": {"given": "Made up", "reports": [{"offset_s": 0, "results": [{"value": 4.2}]}]}}
    payload = seed_payload(people)
    assert json.loads(payload) == people and len(payload) < len(json.dumps(people, indent=2))
    with pytest.raises(ValueError):
        seed_payload({"too_large": "x" * 65536})


@pytest.mark.asyncio
async def test_expired_watch_does_not_start_another_browser_read(tmp_path):
    from wardos.hospital.demo_robot import DemoWatch
    from wardos.record.labs import LabRecord
    clock = Clock()
    called = []
    async def work():
        called.append(True)
    watch = DemoWatch(step=lambda line: None, inject=lambda: None, test="potassium", every_s=60,
                      until=clock.now(), record=LabRecord(tmp_path, "openmrs", "fake"),
                      log_path=tmp_path / "watch.log", stop_path=tmp_path / "STOP",
                      fetch=work, visit=work, ingest=lambda rows: None, now=clock.now)
    await watch._guarded(work, "a check")
    assert not called and watch._stop.is_set()


def test_allowlisted_context_cannot_load_ehospital(tmp_path):
    folder = build_context(tmp_path / "context")
    relative = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()]
    assert not any("ehospital" in p or "/private/" in p or p.endswith(".env") or "/static/" in p for p in relative)
    assert not (folder / "wardos/hospital/otp.py").exists()
    assert (folder / "skills/openmrs/lab.results.latest.json").exists()
    probe = subprocess.run([sys.executable, "-I", "-c",
        "import sys; sys.path[:0] = ['.', 'tools']; "
        "import wardos.hospital.demo_robot; "
        "from wardos.hospital.portals import get_portal; "
        "assert get_portal('openmrs').name == 'openmrs'; "
        "import importlib.util; assert importlib.util.find_spec('wardos.hospital.portals.ehospital') is None; "
        "exec(\"try:\\n get_portal('ehospital')\\nexcept ValueError:\\n pass\\nelse:\\n raise AssertionError('eHospital was accepted')\"); "
        "print('Isolated robot imports passed')"], cwd=folder, capture_output=True, text=True, timeout=45)
    assert probe.returncode == 0, probe.stderr


def test_openmrs_address_is_one_setting(monkeypatch):
    from wardos import config
    from wardos.hospital.portals.openmrs import OpenMRSPortal
    monkeypatch.setenv("WARDOS_OPENMRS_URL", "https://openmrs.example.test/")
    portal = OpenMRSPortal()
    assert portal.base == "https://openmrs.example.test"
    assert portal.origins == ["https://openmrs.example.test"] and portal.firewall_hosts == ["openmrs.example.test"]
    assert portal.home_url == "https://openmrs.example.test/openmrs/spa/home"
    assert config.openmrs_rest_base() == "https://openmrs.example.test/openmrs/ws/rest/v1"
    monkeypatch.delenv("WARDOS_OPENMRS_URL")
    assert OpenMRSPortal().base == config.DEFAULT_OPENMRS_URL and "dev3.openmrs.org" not in OpenMRSPortal().base


def test_openmrs_login_comes_from_the_environment_and_never_defaults(monkeypatch):
    from wardos import config
    from wardos.hospital.portals.openmrs import OpenMRSPortal
    monkeypatch.delenv("OPENMRS_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="OPENMRS_PASSWORD"):
        config.openmrs_login()
    monkeypatch.setenv("OPENMRS_PASSWORD", "made-up-pass")
    assert OpenMRSPortal().demo_credentials == ("admin", "made-up-pass")


def test_maintenance_tool_refuses_any_other_address(monkeypatch):
    from wardos.hospital import demo_site
    monkeypatch.setenv("OPENMRS_PASSWORD", "made-up-pass")
    monkeypatch.setattr(seed, "BASE", "https://dev3.openmrs.org/openmrs/ws/rest/v1")
    with pytest.raises(ValueError, match="private OpenMRS"):
        demo_site.make_site()._call("GET", "/patient")


def test_skill_goto_with_a_path_uses_the_portal_address():
    skill = json.loads((ROOT / "skills/openmrs/nav.home.json").read_text(encoding="utf-8"))
    assert [s["url"] for s in skill["steps"] if s["action"] == "goto"] == ["/openmrs/spa/home"]
    assert not any("dev3.openmrs.org" in p.read_text(encoding="utf-8") for p in (ROOT / "skills/openmrs").glob("*.json"))


class _Locator:
    def __init__(self, page, selector, text=None):
        self.page, self.selector, self.text = page, selector, text

    @property
    def first(self):
        return self

    async def count(self):
        return self.page.count(self.selector, self.text)

    async def is_visible(self):
        return bool(self.page.count(self.selector, self.text))

    async def wait_for(self, **kw):
        if not self.page.count(self.selector, self.text):
            raise AssertionError(f"nothing to wait for: {self.selector}")

    async def fill(self, value):
        self.page.typed.append(self.selector)

    async def check(self, **kw):
        pass

    async def click(self, **kw):
        self.page.clicks.append(self.selector)
        self.page.advance()

    def get_by_text(self, text, **kw):
        return _Locator(self.page, "header-text", text)


class _ArabicLoginPage:
    def __init__(self):
        self.stage, self.clicks, self.typed, self.url = "user", [], [], "https://x.test/openmrs/spa/login"

    def advance(self):
        self.stage = {"user": "password", "password": "location", "location": "home"}[self.stage]
        self.url = "https://x.test/openmrs/spa/" + ("home" if self.stage == "home" else "login")

    def count(self, selector, text=None):
        from wardos.hospital.portals import openmrs as o
        if selector == o.USERNAME:
            return 1
        if selector == o.PASSWORD:
            return int(self.stage == "password")
        if selector == o.SUBMIT:
            return int(self.stage in ("user", "password", "location"))
        if selector == "header-text":
            return int(self.stage == "home")
        return 0

    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_role(self, role, name=None, exact=False):
        if role != "radio":
            raise AssertionError(f"the robot looked for a {role} by its words ({name!r})")
        return _Locator(self, "radio") if self.stage == "location" else _Locator(self, "none")

    def get_by_text(self, text, **kw):
        raise AssertionError("the robot looked for words on the page")

    async def goto(self, *a, **kw):
        pass

    async def evaluate(self, *a, **kw):
        return False


def test_login_finds_buttons_by_type_so_a_language_cannot_break_it():
    import time
    from wardos.hospital.portals import openmrs as o
    page = _ArabicLoginPage()
    header = _Locator(page, "header")
    page.locator = lambda selector: header if selector == "header" else _Locator(page, selector)
    original = page.count
    page.count = lambda selector, text=None: int(page.stage == "location") if selector == "radio" else original(selector, text)

    class Browser:
        async def active(self):
            return page

    class Log:
        def event(self, *a, **kw):
            pass

    asyncio.run(o.OpenMRSPortal(base="https://x.test").login(
        Browser(), username="admin", password="made-up-pass", models=None, otp=None, log=Log(),
        deadline=time.monotonic() + 60))
    assert page.clicks == [o.SUBMIT, o.SUBMIT, o.SUBMIT] and "/home" in page.url


class CountingSite(FakeSite):
    def __init__(self):
        super().__init__()
        self.calls = 0
    def beds(self):
        self.calls += 1
        return super().beds()
    def find_patient(self, person):
        self.calls += 1
        return super().find_patient(person)
    def active_visit(self, uuid):
        self.calls += 1
        return super().active_visit(uuid)
    def lab_encounters(self, uuid):
        self.calls += 1
        return super().lab_encounters(uuid)


AT_PEOPLE = {"12": {"reports": [{"offset_s": 0, "results": [{"concept": seed.POTASSIUM, "value": 4.2}]}]}}
AT_NOW = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)


def test_bed_ready_reads_only_its_bed_and_writes_nothing(store):
    site = CountingSite()
    people = {**deepcopy(AT_PEOPLE), "11": deepcopy(AT_PEOPLE["12"])}
    maintenance = Maintainer(site, people, TaskQueue(store), now=lambda: AT_NOW)
    assert maintenance.bed_ready("12") is True
    assert site.calls == 4 and site.deleted == []
    assert store.get("status", "openmrs") is None
    maintenance.people["12"]["photos_until_offset"] = 0
    assert maintenance.only_new("12", []) == [] and site.calls == 4


@pytest.mark.parametrize("problem", ["changed", "gone", "busy", "old_tag", "down"])
def test_bed_ready_says_no_at_any_doubt(store, problem):
    site, queue = CountingSite(), TaskQueue(store)
    if problem == "changed":
        site.rows[0]["obs"][0]["value"] = 9.9
    elif problem == "gone":
        site.present = False
    elif problem == "busy":
        assert queue.acquire("maintenance", "someone", seconds=300)
    elif problem == "old_tag":
        site.rows.append({"uuid": "old", "encounterDatetime": seed.iso(AT_NOW - timedelta(minutes=16)),
                          "obs": [{"comment": TAG + "task-c"}]})
    else:
        def down():
            raise RuntimeError("not answering")
        site.beds = down
    assert Maintainer(site, deepcopy(AT_PEOPLE), queue, now=lambda: AT_NOW).bed_ready("12") is False


class ReadyMaintenance(Maintenance):
    ready = True
    def bed_ready(self, bed):
        return self.ready


@pytest.mark.asyncio
async def test_get_reports_skips_full_check_only_when_its_bed_is_ready(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = ReadyMaintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path)
    task(store, admission)
    assert await robot.run("task-a") and maintenance.checks == 0
    maintenance.ready = False
    task(store, admission, ident="task-b")
    assert await robot.run("task-b") and maintenance.checks == 1
    assert "No new reports" in store.get("tasks", "task-b")["steps"]


@pytest.mark.asyncio
async def test_watch_always_runs_the_full_check(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = ReadyMaintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path,
                  now=maintenance.clock.now, sleep=maintenance.clock.sleep, minutes=3)
    task(store, admission, kind="watch")
    assert await robot.run("task-a") and maintenance.checks == 1


@pytest.mark.asyncio
async def test_answer_comes_before_the_poke_and_nothing_new_pokes_nobody(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    pokes = []
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path,
                  poke=lambda guest: pokes.append(list(store.get("tasks", "task-a")["steps"])))
    task(store, admission)
    assert await robot.run("task-a")
    assert len(pokes) == 1 and "new report" in pokes[0][-1]
    task(store, admission, ident="task-b")
    assert await robot.run("task-b") and len(pokes) == 1


def _keeper(tmp_path, **kw):
    from wardos.hospital.session import SessionKeeper
    page = SimpleNamespace(url="https://x.test/openmrs/spa/home")
    async def login(browser, **_):
        pass
    async def logged_out(_):
        return False
    async def logout(browser, **_):
        pass
    async def active():
        return page
    portal = SimpleNamespace(name="openmrs", origins=["https://x.test"], health_skill="nav.home", login=login,
                             logout=logout, looks_logged_out=logged_out, home_url=page.url)
    keeper = SessionKeeper(SimpleNamespace(), portal, None, None, profile_dir=tmp_path, library=object(), ledger_dir=tmp_path,
                           credentials=("a", "b"), console=False, **kw)
    keeper.browser = SimpleNamespace(active=active)
    keeper.health_runs = 0
    def runner(log):
        async def run(*a, **k):
            keeper.health_runs += 1
            return SimpleNamespace(status="OK")
        return SimpleNamespace(run=run)
    keeper._runner = runner
    return keeper


_QUIET_LOG = SimpleNamespace(event=lambda *a, **k: None, say=lambda *a, **k: None)


@pytest.mark.asyncio
async def test_fresh_login_skips_a_second_page_load_only_when_asked(tmp_path):
    trusted = _keeper(tmp_path, trust_login_s=60)
    await trusted.ensure_healthy(_QUIET_LOG)
    await trusted.ensure_healthy(_QUIET_LOG)
    assert trusted.logins == 1 and trusted.health_runs == 0
    await trusted.logout(_QUIET_LOG)
    assert trusted._trusted_until == 0
    normal = _keeper(tmp_path)
    await normal.ensure_healthy(_QUIET_LOG)
    assert normal.logins == 1 and normal.health_runs == 1


@pytest.mark.asyncio
async def test_no_logout_page_load_when_closing_a_throwaway_browser(tmp_path):
    keeper = _keeper(tmp_path, logout_on_stop=False)
    async def fail(*a, **k):
        raise AssertionError("the closing browser was logged out")
    keeper.portal.logout = fail
    keeper.context = SimpleNamespace(close=lambda: asyncio.sleep(0))
    await keeper.stop()


@pytest.mark.asyncio
async def test_home_is_reused_once_after_login_then_loaded_again():
    from wardos.hospital.portals import openmrs as o
    loads = []
    page = SimpleNamespace(url="https://x.test/openmrs/spa/home")
    async def goto(url, **kw):
        loads.append(url)
    page.goto = goto
    async def active():
        return page
    browser = SimpleNamespace(active=active, context=SimpleNamespace(pages=[page]), page=None)
    portal = o.OpenMRSPortal(base="https://x.test", fresh_home=True)
    await portal.go_home(browser, log=_QUIET_LOG)
    assert loads == [] and browser.page is page
    await portal.go_home(browser, log=_QUIET_LOG)
    assert len(loads) == 1


def _browser_reader(monkeypatch, *, choose, sign_in):
    from wardos.hospital import demo_robot, session, runlog
    from wardos import models
    stopped = []
    class Keeper:
        def __init__(self, *a, **kw):
            self.kw = kw
        async def start(self):
            pass
        async def ensure_healthy(self, log):
            await sign_in()
        async def stop(self):
            stopped.append(True)
    async def close():
        pass
    client = SimpleNamespace(spent=lambda: {}, aclose=close)
    monkeypatch.setenv("OPENMRS_PASSWORD", "made-up-pass")
    monkeypatch.setattr(models, "get_model_client", lambda settings: client)
    monkeypatch.setattr(session, "SessionKeeper", Keeper)
    monkeypatch.setattr(runlog.RunLog, "create", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(demo_robot, "choose_demo", choose)
    site = SimpleNamespace(find_patient=lambda person: {"display": "FAKE - Demo Wardos"})
    lines = []
    reader = demo_robot.BrowserReader({"bed": "12", "kind": "get_reports"}, site, {"12": {}}, lines.append)
    return reader, lines, stopped


@pytest.mark.asyncio
async def test_meta_choice_and_sign_in_run_side_by_side(monkeypatch):
    order = []
    both = asyncio.Event()
    async def choose(*a):
        order.append("choose")
        if "sign" in order:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        return "lab.results.latest"
    async def sign_in():
        order.append("sign")
        if "choose" in order:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
    reader, lines, _ = _browser_reader(monkeypatch, choose=choose, sign_in=sign_in)
    await reader.__aenter__()
    assert reader.skill == "lab.results.latest" and lines == ["Signed in"] and reader.identifier == "FAKE"
    assert reader.keeper.kw["trust_login_s"] == 90 and reader.keeper.kw["logout_on_stop"] is False


@pytest.mark.asyncio
async def test_refused_meta_choice_stops_the_browser_and_reads_nothing(monkeypatch):
    async def choose(*a):
        raise ValueError("The demo brain did not choose the requested patient and lab skill")
    async def sign_in():
        await asyncio.sleep(30)
    reader, _, stopped = _browser_reader(monkeypatch, choose=choose, sign_in=sign_in)
    with pytest.raises(ValueError):
        await asyncio.wait_for(reader.__aenter__(), 5)
    assert stopped == [True] and not hasattr(reader, "skill")


@pytest.mark.asyncio
async def test_watch_of_another_test_logs_no_new_and_never_injects_danger(store, tmp_path):
    _, admission = make_guest(store)
    maintenance = Maintenance()
    robot = Robot(TaskQueue(store), maintenance, reader=reader_factory(maintenance), private=tmp_path,
                  now=maintenance.clock.now, sleep=maintenance.clock.sleep, minutes=3)
    store.put("tasks", dict(task(store, admission, kind="watch"), test="sodium"))
    assert await robot.run("task-a")
    work = store.get("tasks", "task-a")
    assert work["status"] == "done" and maintenance.injected == 0
    assert any("no new sodium" in line for line in work["steps"]) and not any("DANGER" in line for line in work["steps"])


def test_watch_needs_a_plain_test_name(store):
    _, admission = make_guest(store)
    store.put("tasks", dict(task(store, admission, kind="watch"), test="../x"))
    with pytest.raises(ValueError):
        TaskQueue(store).claim("task-a")


def test_september_spelled_sept_is_read():
    now = datetime(2026, 11, 2, 12, 0)
    rows = [{"cells": {"Test name": "Haemoglobin", "Result date": d, "Value": "7.6 g/dL"}}
            for d in ("13-Sept-2026, 08:27 PM", "13-Sep-2026, 08:27 PM")]
    values, unread = values_from_rows(rows, now)
    assert unread == 0 and [v.collected_at for v in values] == ["2026-09-13T20:27:00"] * 2


def question(store, ident="task-q", text="How many beds are free in Surgery?"):
    row = dict(id=ident, guest="guest-a", kind="hospital_question", question=text, status="waiting", steps=[],
               created_at=datetime.now(timezone.utc).isoformat())
    store.put("tasks", row)
    return row


def asker_factory(answer):
    class Asker:
        usage = {"usd": 0}
        def __init__(self, task, site, step):
            self.step = step
        async def __aenter__(self):
            self.step("Signed in")
            return self
        async def __aexit__(self, *args):
            pass
        async def ask(self, text):
            self.step("Used saved skill: beds vacant count")
            return answer
    return Asker


@pytest.mark.asyncio
async def test_a_guest_question_is_answered_with_the_skill_said(store, tmp_path):
    maintenance = Maintenance()
    answer = {"text": "Surgery has 9 free beds.", "note": "", "skill": "beds.vacant.count", "state": "used"}
    robot = Robot(TaskQueue(store), maintenance, asker=asker_factory(answer), private=tmp_path)
    question(store)
    assert await robot.run("task-q")
    done = store.get("tasks", "task-q")
    assert done["status"] == "done" and done["answer"] == "Surgery has 9 free beds."
    assert "Used saved skill: beds vacant count" in done["steps"]
    assert (done["skill"], done["skill_state"]) == ("beds.vacant.count", "used")


@pytest.mark.asyncio
async def test_a_guest_question_with_no_answer_fails_plainly(store, tmp_path):
    robot = Robot(TaskQueue(store), Maintenance(), asker=asker_factory({"text": "", "note": "x", "skill": None, "state": None}),
                  private=tmp_path)
    question(store)
    assert not await robot.run("task-q")
    assert store.get("tasks", "task-q")["status"] == "failed"


@pytest.mark.asyncio
async def test_a_question_with_an_identifier_is_refused(store, tmp_path):
    robot = Robot(TaskQueue(store), Maintenance(), asker=asker_factory({"text": "x"}), private=tmp_path)
    question(store, text="labs of 20269990001")
    with pytest.raises(ValueError):
        await robot.run("task-q")
